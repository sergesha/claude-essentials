"""Canonical decision receipts extend retained PoC acceptance/publication behavior."""
from dataclasses import replace
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
    return ActionContext(ServiceContext(tmp_path/'service'), request, store.base, evidence, {})


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


def test_native_projection_requires_configuration_not_recording_time(tmp_path, candidate):
    from knowledge_atlas.native_pipeline import execute_native
    from dataclasses import asdict
    store, oid = candidate
    context = context_for(tmp_path, store, oid)
    request = replace(context.request, action='publication.build', payload={'content': oid})
    with pytest.raises(AtlasError) as caught:
        execute_native({'request': asdict(request), 'atlas_path': str(store.base),
                        'evidence': str(context.evidence), 'configuration': {}, 'runtime_root': str(tmp_path/'runtime')})
    assert caught.value.code == 'runtime_unavailable'


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
    assert atlas_status(context).data['matching_decision_receipt'] is False
    assert read_action(history_context).data['history'][0]['matching_decision_receipt'] is False


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


@pytest.mark.parametrize(('spelling','first_channel','action'), [
    ('uppercase', None, 'accept'), ('compact', None, 'reject'), ('canonical', 'http', 'reject')])
def test_git_only_decision_replay_preserves_uuid_channel_and_effect(tmp_path, candidate, monkeypatch,
                                                                    spelling, first_channel, action):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.operations import register_atlas, submit, inspect_operation
    from knowledge_atlas.worker import run_next
    store, oid = candidate
    ctx = context_for(tmp_path,store,oid,action)
    atlas_id = store.read(oid)['manifest']['package_id']
    uid = uuid.UUID('abcd7890-e25a-4a4f-9016-928b61f0bead')
    old_id = {'uppercase':str(uid).upper(), 'compact':uid.hex, 'canonical':str(uid)}[spelling]
    old_request = replace(ctx.request, atlas_id=atlas_id, request_id=old_id)
    config = {'attribution_channel':first_channel} if first_channel else {}
    monkeypatch.setattr(decisions,'build_projection',lambda ctx,c:projection(c))
    original = decide_candidate(replace(ctx,request=old_request,configuration=config))
    receipt = original.data['decision']
    assert receipt['decision_id'] == old_id
    old_refs = git(store.root,'for-each-ref','--format=%(refname) %(objectname)',decisions.DECISIONS)

    def with_empty_service(name, request):
        service = ServiceContext(tmp_path/name, configuration={'attribution_channel':'mcp'})
        register_atlas(service, atlas_id, store.base)
        queued = submit(service, request)
        run_next(service)
        return inspect_operation(service, queued.operation_id)

    replay_request = replace(old_request, request_id=str(uid))
    replay = with_empty_service('restored', replay_request)
    assert replay.error is None and replay.result.data['decision_commit'] == original.data['decision_commit']
    assert replay.result.data['decision'] == receipt
    assert replay.result.publication == original.publication
    changed = replace(replay_request, payload={**replay_request.payload, 'reason':'Changed reason'})
    assert with_empty_service('changed', changed).error['code'] == 'request_conflict'
    digest_only = replace(replay_request, valid_at='2026-11-12T00:00:00Z')
    assert with_empty_service('changed-selection', digest_only).error['code'] == 'request_conflict'
    assert git(store.root,'for-each-ref','--format=%(refname) %(objectname)',decisions.DECISIONS) == old_refs


def test_recovery_from_legacy_accepted_effect_preserves_attribution(tmp_path, candidate, monkeypatch):
    from knowledge_atlas.actions import decisions
    store, oid = candidate
    ctx = context_for(tmp_path,store,oid,'accept')
    ctx = replace(ctx,request=replace(ctx.request,request_id=ctx.request.request_id.upper()))
    monkeypatch.setattr(decisions,'build_projection',lambda ctx,c:projection(c))
    original = decide_candidate(ctx)
    git(store.root,'update-ref','-d', decisions.DECISIONS + ctx.request.request_id)
    retry = replace(ctx,request=replace(ctx.request,request_id=ctx.request.request_id.lower()),
                    configuration={'attribution_channel':'mcp'},previous_evidence=ctx.evidence)
    replay = reconcile_decision(retry)
    assert replay is not None and replay.data['decision'] == original.data['decision']
    assert replay.data['accepted_content'] == original.data['accepted_content']


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
    with pytest.raises(AtlasError) as conflict:
        decide_candidate(ctx)
    assert conflict.value.code == 'request_conflict'
