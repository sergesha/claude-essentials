"""Durable service execution around the retained Git core, without model calls."""
import json
import multiprocessing
from pathlib import Path
import time
import uuid
import pytest

from knowledge_atlas.contracts import ActionRequest, ActionResult, ServiceContext, AtlasError
from knowledge_atlas.operations import submit, inspect_operation, dispatch, resume
from knowledge_atlas.locks import AtlasLock


def request(action='atlas.create', **payload):
    return ActionRequest(action=action, request_id=str(uuid.uuid4()), payload=payload)


def wait_for(context, identifier, state):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = inspect_operation(context, identifier)
        if result.state == state:
            return result
        time.sleep(.02)
    pytest.fail(f'operation did not reach {state}: {result}')


def blocking_handler(context):
    (context.evidence / 'started').write_text('started')
    while not Path(context.request.payload['release']).exists():
        time.sleep(.02)
    return ActionResult(data={'frozen': context.configuration})


def lock_holder(path, started, release):
    with AtlasLock(Path(path)):
        started.set()
        release.wait(10)


def test_same_request_reuses_operation(tmp_path):
    context = ServiceContext(tmp_path / 'service')
    req = request(path=str(tmp_path / 'atlas'))
    first = submit(context, req)
    context.configuration['changed_after_submission'] = True
    second = submit(context, req)
    assert first.operation_id == second.operation_id
    assert json.loads((first.evidence / 'input.json').read_text())['configuration'] == {}


def test_changed_payload_conflicts(tmp_path):
    context = ServiceContext(tmp_path / 'service')
    req = request(path=str(tmp_path / 'atlas'))
    submit(context, req)
    changed = ActionRequest(action=req.action, request_id=req.request_id, payload={'path':str(tmp_path / 'other')})
    with pytest.raises(AtlasError, match='request_conflict'):
        submit(context, changed)


def test_alias_paths_share_writer_lock(tmp_path):
    atlas = tmp_path / 'atlas'; atlas.mkdir()
    alias = tmp_path / 'alias'; alias.symlink_to(atlas, target_is_directory=True)
    process_context = multiprocessing.get_context('spawn')
    started, release = process_context.Event(), process_context.Event()
    process = process_context.Process(target=lock_holder, args=(str(atlas), started, release))
    process.start()
    try:
        assert started.wait(10)
        with pytest.raises(AtlasError, match='busy'):
            with AtlasLock(alias):
                pytest.fail('a second writer entered')
    finally:
        release.set(); process.join(10)
    assert process.exitcode == 0


def test_restart_keeps_live_worker_or_marks_interrupted(tmp_path):
    context = ServiceContext(tmp_path / 'service', handlers={'atlas.create':blocking_handler})
    operation = submit(context, request(path=str(tmp_path/'atlas'), release=str(tmp_path/'release')))
    process = dispatch(context)
    try:
        running = wait_for(context, operation.operation_id, 'running')
        restarted = ServiceContext(context.service_root, handlers=context.handlers)
        assert inspect_operation(restarted, operation.operation_id).state == 'running'
        assert not (tmp_path/'release').exists()
        process.terminate(); process.join(10)
        interrupted = inspect_operation(restarted, operation.operation_id)
        assert interrupted.state == 'interrupted'
        assert interrupted.attempt == running.attempt
        assert (interrupted.evidence / 'input.json').is_file()
    finally:
        if process.is_alive():
            process.terminate(); process.join(10)


def test_create_open_status_preserve_native_identity(tmp_path):
    context = ServiceContext(tmp_path/'service')
    operation = submit(context, request(path=str(tmp_path/'data atlas')))
    process = dispatch(context); process.join(10)
    result = inspect_operation(context, operation.operation_id)
    assert result.state == 'succeeded', result.error
    atlas_id = result.result.data['atlas_id']
    from knowledge_atlas.actions.atlas import atlas_status, open_atlas
    from knowledge_atlas.contracts import ActionContext
    status = atlas_status(ActionContext.for_read(context, ActionRequest('atlas.status', atlas_id=atlas_id)))
    assert status.data['atlas_id'] == atlas_id
    assert status.publication is None
    assert status.data['accepted_content'] == []
    other = ServiceContext(tmp_path/'other-service')
    opened = open_atlas(ActionContext.for_read(other, ActionRequest('atlas.open', payload={'path':str(tmp_path/'data atlas')})))
    assert opened.data['atlas_id'] == atlas_id
    assert opened.publication is None


def test_resume_requires_reconciliation_and_new_workspace(tmp_path):
    context = ServiceContext(tmp_path/'service', handlers={'atlas.create':blocking_handler})
    operation = submit(context, request(path=str(tmp_path/'atlas'), release=str(tmp_path/'release')))
    process = dispatch(context)
    wait_for(context, operation.operation_id, 'running')
    process.terminate(); process.join(10)
    previous = inspect_operation(context, operation.operation_id)
    resumed = resume(context, operation.operation_id, str(uuid.uuid4()))
    assert resumed.attempt == 2
    assert resumed.evidence != previous.evidence
    process = dispatch(context); process.join(10)
    failed = inspect_operation(context, operation.operation_id)
    assert failed.state == 'failed'
    assert failed.error['code'] == 'unsafe_resume'
    assert not (failed.evidence/'started').exists()


def test_queued_configuration_is_frozen_and_status_remains_responsive(tmp_path):
    context = ServiceContext(tmp_path/'service', configuration={'stage':'original'}, handlers={'atlas.create':blocking_handler})
    operation = submit(context, request(path=str(tmp_path/'atlas'), release=str(tmp_path/'release')))
    context.configuration['stage'] = 'changed'
    process = dispatch(context)
    try:
        wait_for(context, operation.operation_id, 'running')
        other = dispatch(context); other.join(10)
        assert other.exitcode == 0
        assert inspect_operation(context, operation.operation_id).state == 'running'
        (tmp_path/'release').touch()
        process.join(10)
        completed = inspect_operation(context, operation.operation_id)
        assert completed.state == 'succeeded'
        assert completed.result.data['frozen'] == {'stage':'original'}
    finally:
        if process.is_alive(): process.terminate(); process.join(10)


def test_dispatcher_drains_queue_and_shutdown_reports_active_work(tmp_path):
    from knowledge_atlas.operations import Dispatcher
    context = ServiceContext(tmp_path/'service', handlers={'atlas.create':blocking_handler})
    first = submit(context, request(path=str(tmp_path/'atlas-one'), release=str(tmp_path/'release')))
    second = submit(context, request(path=str(tmp_path/'atlas-two'), release=str(tmp_path/'release')))
    dispatcher = Dispatcher(context)
    dispatcher.start()
    try:
        wait_for(context, first.operation_id, 'running')
        with pytest.raises(AtlasError, match='busy'):
            dispatcher.stop()
        assert inspect_operation(context, second.operation_id).state == 'queued'
        (tmp_path/'release').touch()
        wait_for(context, second.operation_id, 'succeeded')
    finally:
        (tmp_path/'release').touch()
        dispatcher.stop(wait=True)


def test_stale_empty_atlas_request_has_no_canonical_effect(tmp_path):
    context = ServiceContext(tmp_path/'service')
    operation = submit(context, ActionRequest('atlas.create', request_id=str(uuid.uuid4()),
        expected_publication='a'*40, payload={'path':str(tmp_path/'atlas')}))
    process = dispatch(context); process.join(10)
    result = inspect_operation(context, operation.operation_id)
    assert result.state == 'failed'
    assert result.error['code'] == 'stale_publication'
    assert not (tmp_path/'atlas').exists()


def runtime_handler(context):
    return ActionResult(data={'runtime_root':str(context.service.runtime.runtime_root)})


def test_queued_runtime_configuration_is_frozen(tmp_path):
    from knowledge_atlas.contracts import RuntimeConfig
    context = ServiceContext(tmp_path/'service', runtime=RuntimeConfig(tmp_path/'runtime-original'),
        handlers={'atlas.create':runtime_handler})
    operation = submit(context, request(path=str(tmp_path/'atlas')))
    context.runtime = RuntimeConfig(tmp_path/'runtime-changed')
    process = dispatch(context); process.join(10)
    result = inspect_operation(context, operation.operation_id)
    assert result.state == 'succeeded'
    assert result.result.data['runtime_root'] == str(tmp_path/'runtime-original')


def test_resume_requires_uuid_before_inspecting_operation(tmp_path):
    context = ServiceContext(tmp_path/'service')
    with pytest.raises(AtlasError) as error:
        resume(context, 'not-an-operation', None)
    assert error.value.code == 'invalid_request'
    assert not context.service_root.exists()


def test_concurrent_open_cannot_replace_existing_location(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    import threading
    from knowledge_atlas import operations
    context = ServiceContext(tmp_path/'service')
    first, second = tmp_path/'first', tmp_path/'second'
    first.mkdir(); second.mkdir()
    native_database = operations.database
    with native_database(context): pass
    barrier = threading.Barrier(2)

    @contextmanager
    def simultaneous_reads(service):
        with native_database(service) as connection:
            def trace(statement):
                # Schedule both unprotected reads together; protected reads
                # already serialize on SQLite's real write transaction.
                if statement.startswith('SELECT path FROM atlases') and not connection.in_transaction:
                    barrier.wait(timeout=5)
            connection.set_trace_callback(trace)
            yield connection
    monkeypatch.setattr(operations, 'database', simultaneous_reads)

    def register(path):
        try:
            operations.register_atlas(context, 'one-native-uuid', path)
            return 'registered'
        except AtlasError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register, (first, second)))
    assert sorted(outcomes) == ['atlas_conflict', 'registered']


def test_mutation_uuid_aliases_preserve_legacy_operation_and_resume(tmp_path):
    from dataclasses import asdict, replace
    import hashlib
    from knowledge_atlas.operations import database, encoded

    service = ServiceContext(tmp_path / 'service')
    req = request(path=str(tmp_path / 'atlas'))
    uppercase = replace(req, request_id=req.request_id.upper())
    operation = submit(service, uppercase)
    assert json.loads((operation.evidence / 'input.json').read_text())['request']['request_id'] == req.request_id
    assert submit(service, req) == operation
    legacy = encoded(asdict(uppercase))
    with database(service) as db:
        db.execute('UPDATE operations SET request_id=?,request=?,digest=?,state=? WHERE operation_id=?',
                   (uppercase.request_id, legacy, hashlib.sha256(legacy.encode()).hexdigest(), 'failed', operation.operation_id))
    assert submit(service, req).operation_id == operation.operation_id
    with pytest.raises(AtlasError, match='request_conflict'):
        submit(service, replace(req, payload={'path': 'different'}))
    with pytest.raises(AtlasError, match='request_conflict'):
        resume(service, operation.operation_id, req.request_id)
    key = str(uuid.uuid4())
    resumed = resume(service, operation.operation_id, key.upper())
    with database(service) as db:
        db.execute('UPDATE resumes SET request_id=? WHERE request_id=?', (key.upper(), key))
    assert resume(service, operation.operation_id, key) == resumed
    with pytest.raises(AtlasError, match='request_conflict'):
        submit(service, replace(req, request_id=key))


def test_nested_atlas_rejected_before_writing_inside_existing_worktree(tmp_path):
    from knowledge_atlas.worker import run_next

    service = ServiceContext(tmp_path / 'service')
    parent = tmp_path / 'outer'
    operation = submit(service, request(path=str(parent)))
    run_next(service)
    assert inspect_operation(service, operation.operation_id).state == 'succeeded'
    nested = parent / 'atlas' / 'nested'
    # A second service has never registered this atlas.
    fresh = ServiceContext(tmp_path / 'fresh')
    with pytest.raises(AtlasError, match='nested atlas'):
        submit(fresh, request(path=str(nested)))
    assert not nested.exists()


def test_bound_atlas_path_guards_reads_mutations_and_queued_workers(tmp_path):
    from knowledge_atlas.operations import atlas_path, register_atlas
    from knowledge_atlas.worker import run_next

    service = ServiceContext(tmp_path / 'service')
    other = tmp_path / 'other'
    queued = submit(service, request(path=str(other)))
    register_atlas(service, 'existing', other)
    bound = tmp_path / 'atlas'
    service.configuration['atlas_path'] = str(bound)
    assert atlas_path(service, request(path=str(bound))) == bound
    for action in ('atlas.create', 'atlas.open', 'archive.restore'):
        with pytest.raises(AtlasError, match='bound'):
            atlas_path(service, request(action, path=str(other)))
    for action in ('atlas.status', 'candidate.decide'):
        with pytest.raises(AtlasError, match='bound'):
            atlas_path(service, ActionRequest(action, atlas_id='existing'))
    run_next(service)
    failed = inspect_operation(service, queued.operation_id)
    assert failed.state == 'failed' and failed.error['code'] == 'invalid_location'
    assert not other.exists()
