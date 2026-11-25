"""Canonical decision receipts extend retained PoC acceptance/publication behavior."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid
import pytest
from knowledge_atlas.contracts import ActionContext, ActionRequest, ServiceContext, AtlasError
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.actions.decisions import decide_candidate, inspect_candidate, reconcile_decision, revise_candidate


@pytest.fixture
def candidate(tmp_path):
    store = GitStore(tmp_path/'atlas')
    fixtures = Path(__file__).parent/'fixtures'
    wiki = json.loads((fixtures/'evidence/wiki-initial-final.json').read_text())
    oid = store.snapshot(wiki, {'entities':[],'statements':[]}, {}, recorded_at='2026-11-13T00:00:00Z')['commit']
    return store, oid


def context_for(tmp_path, store, candidate, action='reject', expected=None):
    evidence=tmp_path/str(uuid.uuid4()); evidence.mkdir()
    request=ActionRequest('candidate.decide', request_id=str(uuid.uuid4()), expected_publication=expected,
        payload={'candidate':candidate,'action':action,'actor':'Fixture','kind':'synthetic',
                 'reason':'Exercise canonical decision persistence','decided_at':'2026-11-13T01:00:00Z'})
    return ActionContext(ServiceContext(tmp_path/'service'), request, store.base, evidence, {'attribution_channel': 'python'})


def projection(content):
    from knowledge_atlas.runtime import _assets
    profile=json.loads((_assets()/'embedding-profile.json').read_text())
    return {'content_commit':content,'graph_commit':'controlled-test-projection','search_checkpoint':'controlled-test-projection',
            'ready':True,'profile_id':profile['profile_id'],'test_only':True}


def test_rejection_is_canonical_without_sqlite(tmp_path, candidate):
    store, oid=candidate
    context=context_for(tmp_path,store,oid)
    result=decide_candidate(context)
    assert result.data['decision']['action']=='reject'
    assert Publications(store).ready() is None
    fresh=replace(context, service=ServiceContext(tmp_path/'fresh-service'))
    inspected=inspect_candidate(fresh)
    assert inspected.data['decisions'][0]['action']=='reject'
    assert inspected.data['decisions'][0]['kind']=='synthetic'
    assert not fresh.service.service_root.exists()


def test_receipt_tree_has_valid_package_closure(tmp_path, candidate):
    store,oid=candidate
    result=decide_candidate(context_for(tmp_path,store,oid))
    receipt=result.data['decision_commit']
    assert store.read(receipt)['manifest']['package_id']==store.read(oid)['manifest']['package_id']
    git(store.root,'merge-base','--is-ancestor',oid,receipt)
    assert store.closure(receipt)


def test_future_decision_and_preupgrade_pending_replay(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    store, oid = candidate
    context = context_for(tmp_path, store, oid, 'accept')
    payload = {**context.request.payload, 'decided_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    context = replace(context, request=replace(context.request, payload=payload))
    with pytest.raises(AtlasError) as error:
        decide_candidate(context)
    assert (error.value.code, error.value.status) == ('invalid_acceptance_time', 422)
    assert not git(store.root, 'for-each-ref', 'refs/knowledge-atlas/decisions/')

    def unavailable(*_):
        raise RuntimeError('fixture projection unavailable')

    with monkeypatch.context() as old:
        old.setattr(Publications, '_check_acceptance_time', staticmethod(lambda *_: None))
        old.setattr(decisions, 'build_projection', unavailable)
        pending = decide_candidate(context)
    assert pending.data['status'] == 'accepted_pending_publication'
    monkeypatch.setattr(decisions, 'build_projection', lambda *_: pytest.fail('unexpected projection'))
    replay = reconcile_decision(replace(context, previous_evidence=context.evidence))
    assert replay.data['publication_error']['code'] == 'invalid_acceptance_time'
    assert replay.data['decision_commit'] == pending.data['decision_commit']
    assert Publications(store).ready() is None


def test_stale_accept_preserves_winner(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    store,oid=candidate
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:projection(content))
    first=decide_candidate(context_for(tmp_path,store,oid,'accept'))
    current=first.publication
    assert current is not None
    with pytest.raises(AtlasError,match='stale_publication'):
        decide_candidate(context_for(tmp_path,store,oid,'accept'))
    assert Publications(store).ready()==current
    with pytest.raises(AtlasError) as stale:
        decide_candidate(context_for(tmp_path,store,oid,'accept',current))
    assert stale.value.code == 'stale_proposal' and stale.value.status == 409
    assert Publications(store).ready()==current


def test_lost_ack_resume_reuses_decision(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    store,oid=candidate
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:projection(content))
    context=context_for(tmp_path,store,oid,'accept')
    original=decide_candidate(context)
    monkeypatch.setattr(decisions,'build_projection',lambda *_:pytest.fail('published effect repeated'))
    resumed=reconcile_decision(replace(context,previous_evidence=context.evidence))
    assert resumed.data['decision_commit']==original.data['decision_commit']
    assert resumed.data['accepted_content']==original.data['accepted_content']
    assert resumed.publication==original.publication
    refs=git(store.root,'for-each-ref','--format=%(objectname)','refs/knowledge-atlas/decisions/').splitlines()
    assert refs==[original.data['decision_commit']]


def test_failed_projection_keeps_previous_ready(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    store,oid=candidate
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:projection(content))
    first=decide_candidate(context_for(tmp_path,store,oid,'accept'))
    def failed(*_): raise RuntimeError('controlled projection failure')
    monkeypatch.setattr(decisions,'build_projection',failed)
    from knowledge_atlas.core.knowledge import propose_records
    revision = propose_records(store, first.data['accepted_content'], [], actor='Fixture', method='Fixture',
        reason='New revision', recorded_at='2026-11-13T01:00:00Z', base_publication=first.publication)
    second=decide_candidate(context_for(tmp_path,store,revision,'accept',first.publication))
    assert second.data['accepted_content']
    assert second.data['status']=='accepted_pending_publication'
    assert second.data['publication_error']
    assert Publications(store).ready()==first.publication


def test_revision_retains_receipt_and_original_candidate(tmp_path,candidate):
    store,oid=candidate
    original=store.read(oid)
    context=context_for(tmp_path,store,oid)
    request=replace(context.request,action='candidate.revise',payload={
        'candidate':oid,'entries':[],'actor':'Fixture','kind':'synthetic','reason':'Review revised candidate',
        'decided_at':'2026-11-13T01:00:00Z'})
    result=revise_candidate(replace(context,request=request))
    assert result.data['candidate']!=oid
    assert store.read(oid)==original
    assert result.data['decision']['action']=='revise'
    git(store.root,'merge-base','--is-ancestor',result.data['candidate'],result.data['decision_commit'])
    assert Publications(store).ready() is None


def test_inspection_is_json_serializable(tmp_path, candidate):
    from dataclasses import asdict
    store, oid = candidate
    result = inspect_candidate(context_for(tmp_path, store, oid))
    encoded = json.dumps(asdict(result))
    assert json.loads(encoded)['data']['candidate'] == oid


def test_decide_rejects_revision_action_without_writing_receipt(tmp_path, candidate):
    store, oid = candidate
    with pytest.raises(AtlasError, match='invalid_request'):
        decide_candidate(context_for(tmp_path, store, oid, 'revise'))
    assert not git(store.root, 'for-each-ref', 'refs/knowledge-atlas/decisions/')


def test_registered_worker_recovers_published_decision(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions, register_handlers
    from knowledge_atlas.worker import run_attempt
    store, oid = candidate
    monkeypatch.setattr(decisions, 'build_projection', lambda context, content: projection(content))
    context = context_for(tmp_path, store, oid, 'accept')
    handler = register_handlers(context.service).get('candidate.decide')
    assert handler is not None
    original = run_attempt(context, handler)
    resumed = run_attempt(replace(context, previous_evidence=context.evidence), handler)
    assert resumed.publication == original.publication
    assert resumed.canonical_effects == original.canonical_effects


def test_native_dispatch_requires_service_root(tmp_path, candidate):
    from knowledge_atlas.native_pipeline import execute_native
    from dataclasses import asdict
    store, oid = candidate
    context = context_for(tmp_path, store, oid)
    request = replace(context.request, action='publication.build', payload={'content': oid})
    with pytest.raises(AtlasError) as caught:
        execute_native({'request': asdict(request), 'atlas_path': str(store.base),
                        'evidence': str(context.evidence), 'configuration': {}, 'runtime_root': str(tmp_path/'runtime')})
    assert caught.value.code == 'invalid_configuration'


def test_operation_can_resume_accepted_pending_publication(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.operations import register_atlas, submit, inspect_operation, resume
    from knowledge_atlas.worker import run_next
    store, oid = candidate
    context = context_for(tmp_path, store, oid, 'accept')
    atlas_id = store.read(oid)['manifest']['package_id']
    register_atlas(context.service, atlas_id, store.base)
    request = replace(context.request, atlas_id=atlas_id)
    def unavailable(*_): raise RuntimeError('temporary native failure')
    monkeypatch.setattr(decisions, 'build_projection', unavailable)
    operation = submit(context.service, request)
    run_next(context.service)
    pending = inspect_operation(context.service, operation.operation_id)
    assert pending.result.data['status'] == 'accepted_pending_publication'
    from knowledge_atlas.actions.atlas import atlas_status
    git(store.root, 'update-ref', 'refs/poc9/proposals/' + oid, oid)
    assert atlas_status(context).data['candidate_statuses'][oid] == 'accepted_pending_publication'
    original_content = pending.result.data['accepted_content']
    monkeypatch.setattr(decisions, 'build_projection', lambda context, content: projection(content))
    resumed = resume(context.service, operation.operation_id, str(uuid.uuid4()))
    assert resumed.state == 'queued'
    run_next(context.service)
    finished = inspect_operation(context.service, operation.operation_id)
    assert finished.result.data['status'] == 'published'
    assert finished.result.data['accepted_content'] == original_content
    assert finished.result.data['decision_commit'] == pending.result.data['decision_commit']
    assert finished.attempt == 2


def test_decisions_skip_unrelated_closure_validation_and_match_selected_publication(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.actions.atlas import atlas_status
    from knowledge_atlas.actions.reads import read_action
    from knowledge_atlas.core.git_store import GitStore
    store, oid = candidate
    git(store.root, 'update-ref', 'refs/poc9/proposals/'+oid, oid)
    previous = decide_candidate(context_for(tmp_path,store,oid))
    reject_oid = previous.data['decision_commit']
    real_read = GitStore.read
    def targeted_read(self, commit):
        assert commit != reject_oid, 'unrelated decision was fully revalidated'
        return real_read(self, commit)
    context = context_for(tmp_path,store,oid,'accept')
    with monkeypatch.context() as local:
        local.setattr(GitStore, 'read', targeted_read)
        local.setattr(decisions,'build_projection',lambda ctx,c:{**projection(c), 'database':'admin/fixture'})
        accepted = decide_candidate(context)
    assert accepted.publication
    status = atlas_status(context).data
    assert status['candidate_statuses'][oid] == 'accepted'
    assert status['matching_decision_receipt'] is True
    history_context = replace(context, request=ActionRequest('history'))
    assert read_action(history_context).data['history'][0]['matching_decision_receipt'] is True
    git(store.root,'update-ref','-d', decisions.DECISIONS + context.request.request_id)
    with pytest.raises((ValueError, AtlasError), match='decision'):
        atlas_status(context)
    with pytest.raises(AtlasError, match='decision'):
        read_action(history_context)


def test_new_decision_can_reconsider_rejection_on_the_same_base(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions.decisions import decision_inventory
    store, oid = candidate
    rejected = decide_candidate(context_for(tmp_path, store, oid))
    accepted = context_for(tmp_path, store, oid, 'accept')
    accepted = replace(accepted, configuration={'attribution_channel':'viewer'})
    monkeypatch.setattr('knowledge_atlas.actions.decisions.build_projection', lambda ctx, content: projection(content))
    published = decide_candidate(accepted)
    assert rejected.data['status'] == 'rejected'
    assert published.data['status'] == 'published'
    assert Publications(store).read(published.publication)['acceptance_channel'] == 'viewer'
    assert {entry['receipt']['action'] for entry in decision_inventory(store)} == {'reject','accept'}


def test_git_only_completed_replay_preserves_channel_and_effect(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.operations import register_atlas, submit, inspect_operation
    from knowledge_atlas.worker import run_next
    store, oid = candidate
    ctx = context_for(tmp_path,store,oid,'reject')
    atlas_id = store.read(oid)['manifest']['package_id']
    old_request = replace(ctx.request, atlas_id=atlas_id)
    original = decide_candidate(replace(ctx,request=old_request,configuration={'attribution_channel':'http'}))
    receipt = original.data['decision']
    old_refs = git(store.root,'for-each-ref','--format=%(refname) %(objectname)',decisions.DECISIONS)

    def with_empty_service(name, request):
        service = ServiceContext(tmp_path/name, configuration={'attribution_channel':'mcp'})
        register_atlas(service, atlas_id, store.base)
        queued = submit(service, request)
        run_next(service)
        return inspect_operation(service, queued.operation_id)

    replay_request = old_request
    replay = with_empty_service('restored', replay_request)
    assert replay.error is None and replay.result.data['decision_commit'] == original.data['decision_commit']
    assert replay.result.data['decision'] == receipt
    assert replay.result.publication == original.publication
    changed = replace(replay_request, payload={**replay_request.payload, 'reason':'Changed reason'})
    assert with_empty_service('changed', changed).error['code'] == 'request_conflict'
    digest_only = replace(replay_request, valid_at='2026-11-12T00:00:00Z')
    assert with_empty_service('changed-selection', digest_only).error['code'] == 'request_conflict'
    assert git(store.root,'for-each-ref','--format=%(refname) %(objectname)',decisions.DECISIONS) == old_refs


def stop_worker_after_review(service):
    import os
    import signal
    from unittest.mock import patch
    from knowledge_atlas.worker import run_next

    def stop_before_receipt(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGKILL)
    with patch('knowledge_atlas.actions.decisions._write_receipt', stop_before_receipt):
        run_next(service)


def test_interrupted_acceptance_requires_explicit_resume_with_original_attribution(tmp_path, candidate, monkeypatch, request):
    from dataclasses import asdict
    import multiprocessing
    import signal
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.actions.atlas import atlas_status
    from knowledge_atlas.api_models import Operation
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.http_api import create_app
    from knowledge_atlas.operations import Dispatcher, register_atlas
    from knowledge_atlas.worker import run_next
    from test_interfaces import raw

    store, oid = candidate
    ctx = context_for(tmp_path, store, oid, 'accept')
    atlas_id = store.read(oid)['manifest']['package_id']
    mutation = replace(ctx.request, atlas_id=atlas_id)
    register_atlas(ctx.service, atlas_id, store.base)
    # Drive the worker at the exact crash boundary. Both clients use the real HTTP admission path.
    monkeypatch.setattr(Dispatcher, 'start', lambda self: None)
    app = create_app(ctx.service).start()
    status, submitted = raw(app, mutation.action, asdict(mutation))
    assert status == 202
    op_id = submitted['operation_id']
    worker = multiprocessing.get_context('spawn').Process(target=stop_worker_after_review, args=(ctx.service,))
    worker.start()
    worker.join(30)
    if worker.is_alive():
        worker.kill()
        worker.join()
        pytest.fail('worker failed to reach the review boundary')
    assert worker.exitcode == -signal.SIGKILL
    assert AtlasClient(ctx.service.service_root).operation(op_id).state == 'interrupted'
    app.stop()

    restarted = create_app(ServiceContext(ctx.service.service_root)).start()
    request.addfinalizer(restarted.stop)
    client = AtlasClient(restarted.context.service_root)
    git(store.root, 'update-ref', 'refs/poc9/proposals/' + oid, oid)
    pending = atlas_status(ctx).data
    assert pending['candidate_statuses'][oid] == 'accepted_pending_decision'
    content, = pending['accepted_content']
    assert not decisions.decision_inventory(store)
    replay = client.call(mutation)
    assert (replay.operation_id, replay.attempt, replay.state) == (op_id, 1, 'interrupted')
    with pytest.raises(AtlasError, match='request_conflict'):
        client.call(replace(mutation, payload={**mutation.payload, 'reason': 'Different review'}))
    selection = Operation(operation_id=op_id)
    with pytest.raises(AtlasError, match='request_conflict'):
        client.operation_resume(selection, request_id=mutation.request_id)
    resume_id = str(uuid.uuid4())
    assert client.operation_resume(selection, request_id=resume_id).attempt == 2
    with pytest.raises(AtlasError, match='request_conflict'):
        client.call(replace(mutation, request_id=resume_id))
    monkeypatch.setattr(decisions, 'build_projection', lambda ctx, c: projection(c))
    run_next(restarted.context)
    finished = client.operation(op_id)
    assert finished.result.data['status'] == 'published'
    assert finished.result.data['accepted_content'] == content
    decision = finished.result.data['decision']
    assert decision['attribution_channel'] == 'http'
    assert decision['decision_id'] == mutation.request_id and decision['operation_id'] == op_id
    refs = git(store.root, 'show-ref')
    assert client.operation_resume(selection, request_id=resume_id) == finished
    run_next(restarted.context)
    assert refs == git(store.root, 'show-ref')
    assert len(decisions.decision_inventory(store)) == 1


def test_distinct_legacy_receipts_with_equivalent_uuids_require_inspection(tmp_path, candidate):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.core.publication import commit_files
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid)
    original = decide_candidate(ctx)
    alias = '{' + ctx.request.request_id.replace('-', '').upper() + '}'
    receipt = {**original.data['decision'], 'decision_id':alias}
    alias_oid = commit_files(store.root, oid, {'atlas-decision.json':receipt}, [oid], 'Legacy duplicate receipt')
    git(store.root, 'update-ref', decisions.DECISIONS + alias, alias_oid)
    with pytest.raises(AtlasError, match='invalid_decision'):
        decisions.decision_inventory(store)
    assert decide_candidate(ctx).data['decision_commit'] == original.data['decision_commit']


def test_uncertain_acceptance_cannot_be_repeated_on_resume(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.operations import inspect_operation, register_atlas, resume, submit
    from knowledge_atlas.worker import run_next
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid, 'accept')
    atlas_id = store.read(oid)['manifest']['package_id']
    register_atlas(ctx.service, atlas_id, store.base)
    op = submit(ctx.service, replace(ctx.request, atlas_id=atlas_id))
    with monkeypatch.context() as interrupted:
        def crash(*args):
            raise SystemExit('worker lost before durable outcome')
        interrupted.setattr(Publications, 'review', crash)
        with pytest.raises(SystemExit):
            run_next(ctx.service)
    resume(ctx.service, op.operation_id, str(uuid.uuid4()))
    run_next(ctx.service)
    assert inspect_operation(ctx.service, op.operation_id).error['code'] == 'unsafe_resume'
    assert not decisions.decision_inventory(store)
    assert Publications(store).ready() is None


@pytest.mark.parametrize('field', ['decision_id', 'attribution_channel', 'request_sha256', 'actor', 'reason', 'kind', 'decided_at'])
def test_completed_history_refuses_missing_or_mismatched_evidence(tmp_path, candidate, monkeypatch, field):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.core.publication import commit_files
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid, 'accept')
    monkeypatch.setattr(decisions, 'build_projection', lambda ctx, content: projection(content))
    original = decide_candidate(ctx)
    entry, = decisions.decision_inventory(store)
    malformed = entry['receipt'].copy()
    if field == 'request_sha256':
        malformed[field] = 'f' * 64
    else:
        del malformed[field]
    broken = commit_files(store.root, entry['commit'], {'atlas-decision.json': malformed},
                          [entry['commit']], 'Fixture with inconsistent decision')
    git(store.root, 'update-ref', entry['ref'], broken)
    with pytest.raises(ValueError, match='decision'):
        Publications(store).read(original.publication)


@pytest.mark.parametrize('cache', [None, 'foreign'])
def test_native_dispatch_requires_current_vector_cache(tmp_path, candidate, cache):
    from dataclasses import asdict
    from knowledge_atlas.native_pipeline import execute_native
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid)
    payload = {'request': asdict(replace(ctx.request, action='publication.build')),
               'atlas_path': str(store.base), 'configuration': {}, 'evidence': str(ctx.evidence),
               'service_root': str(ctx.service.service_root)}
    if cache:
        payload['vector_cache'] = str(tmp_path / cache)
    with pytest.raises(AtlasError, match='invalid_configuration'):
        execute_native(payload)

def test_another_operation_cannot_resume_a_foreign_interrupted_acceptance(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.operations import inspect_operation, register_atlas, resume, submit
    from knowledge_atlas.worker import run_next
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid, 'accept')
    atlas_id = store.read(oid)['manifest']['package_id']
    req = replace(ctx.request, atlas_id=atlas_id)
    other = ServiceContext(tmp_path / 'original-service', configuration={'attribution_channel': 'viewer'})
    register_atlas(other, atlas_id, store.base)
    submit(other, req)
    with monkeypatch.context() as interrupted:
        def crash(*args, **kwargs):
            raise SystemExit('worker stopped after recording review')
        interrupted.setattr(decisions, '_write_receipt', crash)
        with pytest.raises(SystemExit):
            run_next(other)
    foreign = ServiceContext(tmp_path / 'foreign-service', configuration={'attribution_channel': 'viewer'})
    register_atlas(foreign, atlas_id, store.base)
    foreign_op = submit(foreign, req)
    run_next(foreign)
    outcome = inspect_operation(foreign, foreign_op.operation_id)
    assert outcome.state == 'failed' and outcome.error['code'] == 'unsafe_resume'
    resume(foreign, foreign_op.operation_id, str(uuid.uuid4()))
    run_next(foreign)
    outcome = inspect_operation(foreign, foreign_op.operation_id)
    assert outcome.state == 'failed' and outcome.error['code'] == 'unsafe_resume'
    assert not decisions.decision_inventory(store)

def test_accepted_decision_inventory_requires_its_own_review(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.core.publication import commit_files
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid, 'accept')
    def unavailable(*_):
        raise ValueError('fixture projection unavailable')
    monkeypatch.setattr(decisions, 'build_projection', unavailable)
    pending = decide_candidate(ctx)
    entry, = decisions.decision_inventory(store)
    original = entry['receipt']
    forged = {**original, 'attribution_channel': 'http'}
    broken = commit_files(store.root, oid, {'atlas-decision.json': forged},
                          [oid, pending.data['accepted_content']], 'Conflicting pending attribution')
    git(store.root, 'update-ref', entry['ref'], broken)
    with pytest.raises(AtlasError, match='invalid_decision'):
        decisions.decision_inventory(store)


@pytest.mark.parametrize('existing', [False, True])
def test_native_dispatch_refuses_foreign_service_with_matching_cache(tmp_path, candidate, monkeypatch, existing):
    from dataclasses import asdict
    from knowledge_atlas import native_pipeline
    from knowledge_atlas.operations import initialize_service_storage, register_atlas
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid)
    atlas_id = store.read(oid)['manifest']['package_id']
    register_atlas(ctx.service, atlas_id, store.base)
    foreign = ServiceContext(tmp_path / 'other-service')
    if existing:
        initialize_service_storage(foreign)
    request = replace(ctx.request, action='publication.build', atlas_id=atlas_id, payload={'content':oid})
    payload = {'request':asdict(request), 'configuration':{}, 'evidence':str(ctx.evidence),
               'service_root':str(foreign.service_root), 'vector_cache':str(foreign.service_root/'vectors'),
               'atlas_path':str(store.base)}
    monkeypatch.setattr(native_pipeline, 'project_native', lambda *_: pytest.fail('foreign root reached projection'))
    with pytest.raises(AtlasError, match='invalid_configuration|atlas_not_found|atlas_conflict'):
        native_pipeline.execute_native(payload)
    assert foreign.service_root.exists() == existing
    assert not (foreign.service_root/'vectors').exists()


def test_native_projection_uses_the_running_operations_own_registry(tmp_path, candidate, monkeypatch):
    from dataclasses import asdict
    from knowledge_atlas import native_pipeline
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.contracts import ActionResult
    from knowledge_atlas.operations import inspect_operation, register_atlas, submit
    from knowledge_atlas.worker import run_next
    store, oid = candidate
    ctx = context_for(tmp_path, store, oid, 'accept')
    service = ServiceContext(tmp_path/'primary-service', configuration={'graph':{'url':'http://127.0.0.1:1'}})
    other = ServiceContext(tmp_path/'other-service', configuration=service.configuration)
    identity = store.read(oid)['manifest']['package_id']
    for owner in (service, other):
        register_atlas(owner, identity, store.base)
    def build(context, content):
        native = replace(context, request=replace(context.request, action='publication.build', payload={'content':content}))
        native_pipeline.claim_projection_database(native)
        payload = {'request':asdict(native.request), 'configuration':native.configuration,
                   'atlas_path':str(store.base), 'evidence':str(native.evidence), 'operation_id':native.operation_id}
        with pytest.raises(AtlasError, match='running operation'):
            native_pipeline.execute_native({**payload, 'service_root':str(other.service_root),
                                           'vector_cache':str(other.service_root/'vectors')})
        return native_pipeline.execute_native({**payload, 'service_root':str(service.service_root),
                                              'vector_cache':str(service.service_root/'vectors')}).data
    monkeypatch.setattr(decisions, 'build_projection', build)
    monkeypatch.setattr(native_pipeline, 'project_native',
                        lambda payload, store: ActionResult(data=projection(payload['request']['payload']['content'])))
    op = submit(service, replace(ctx.request, atlas_id=identity))
    run_next(service)
    result = inspect_operation(service, op.operation_id)
    assert result.result.data['status'] == 'published', result
