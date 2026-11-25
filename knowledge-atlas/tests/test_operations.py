"""Durable service execution around the retained Git core, without model calls."""
import json
import multiprocessing
from pathlib import Path
import time
import uuid
import pytest

from acceptance_fixture import review_acceptance
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
    request_open = ActionRequest('atlas.open', atlas_id=atlas_id)
    opened = open_atlas(ActionContext.for_read(context, request_open))
    assert opened.data['atlas_id'] == atlas_id
    assert opened.publication is None
    other = ServiceContext(tmp_path/'other-service')
    with pytest.raises(AtlasError, match='atlas_not_found'):
        ActionContext.for_read(other, request_open)


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


def test_operation_read_can_observe_committed_state_while_writer_is_active(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    service = ServiceContext(tmp_path / 'service')
    queued = submit(service, request(path=str(tmp_path / 'project')))
    with ThreadPoolExecutor(max_workers=1) as readers:
        with closing(sqlite3.connect(service.service_root / 'operations.sqlite3')) as writer:
            writer.execute("UPDATE operations SET state='running' WHERE operation_id=?", (queued.operation_id,))
            read = readers.submit(inspect_operation, service, queued.operation_id)
            assert read.result(timeout=2).state == 'queued'


def test_idle_dispatcher_does_not_repeatedly_open_database(tmp_path, monkeypatch):
    import threading
    from knowledge_atlas import operations
    context = ServiceContext(tmp_path / 'service')
    operations.initialize_service_storage(context)
    first_scan = threading.Event()
    another_scan = threading.Event()
    database = operations.database

    def track_scan(service):
        if threading.current_thread().name == 'atlas-dispatcher':
            (another_scan if first_scan.is_set() else first_scan).set()
        return database(service)

    monkeypatch.setattr(operations, 'database', track_scan)
    dispatcher = operations.Dispatcher(context)
    dispatcher.start()
    try:
        assert first_scan.wait(3)
        assert not another_scan.wait(.35)
    finally:
        dispatcher.stop()


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


def test_concurrent_registration_cannot_replace_existing_location(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    import shutil
    import threading
    from knowledge_atlas import operations
    from knowledge_atlas.core.git_store import GitStore
    from test_packages import snapshot

    context = ServiceContext(tmp_path/'service')
    first, second = tmp_path/'first', tmp_path/'second'
    store = GitStore(first)
    atlas_id = store.read(snapshot(store))['manifest']['package_id']
    shutil.copytree(first, second)
    native_database = operations.database
    with native_database(context): pass
    barrier = threading.Barrier(2)

    @contextmanager
    def simultaneous_reads(service):
        with native_database(service) as connection:
            def trace(statement):
                # Schedule both unprotected reads together; protected reads
                # already serialize on SQLite's real write transaction.
                if statement.startswith('SELECT * FROM atlases WHERE atlas_id=') and not connection.in_transaction:
                    barrier.wait(timeout=5)
            connection.set_trace_callback(trace)
            yield connection
    monkeypatch.setattr(operations, 'database', simultaneous_reads)

    def register(path):
        try:
            operations.register_atlas(context, atlas_id, path)
            return 'registered'
        except AtlasError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register, (first, second)))
    assert sorted(outcomes) == ['atlas_conflict', 'registered']


@pytest.mark.parametrize('spelling', ['uppercase', 'compact', 'braced', 'urn'])
def test_mutation_and_resume_require_canonical_uuid(tmp_path, spelling):
    from dataclasses import replace
    service = ServiceContext(tmp_path / 'service')
    req = request(path=str(tmp_path / 'atlas'))
    uid = uuid.UUID(req.request_id)
    alias = {'uppercase': str(uid).upper(), 'compact': uid.hex, 'braced': '{'+str(uid)+'}', 'urn': uid.urn}[spelling]
    with pytest.raises(AtlasError, match='invalid_request'):
        submit(service, replace(req, request_id=alias))
    op = submit(service, req)
    with pytest.raises(AtlasError, match='invalid_request'):
        resume(service, op.operation_id, alias)
    assert submit(service, req) == op


def test_mutation_rejects_an_inconsistent_retained_digest(tmp_path):
    from knowledge_atlas.operations import database
    service = ServiceContext(tmp_path / 'service')
    req = request(path=str(tmp_path / 'atlas'))
    op = submit(service, req)
    with database(service) as db:
        db.execute('UPDATE operations SET digest=? WHERE operation_id=?', ('0'*64, op.operation_id))
    with pytest.raises(AtlasError, match='request_conflict|unsupported_storage'):
        submit(service, req)


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


@pytest.mark.parametrize('entry', ['launcher', 'serve', 'worker'])
@pytest.mark.parametrize('damage', ['atlas_path', 'old-schema', 'attempts'])
def test_unsupported_startup_leaves_storage_and_workers_untouched(tmp_path, monkeypatch, entry, damage):
    import sqlite3
    from knowledge_atlas import launcher
    from knowledge_atlas.http_api import create_app
    from knowledge_atlas.worker import run_next
    root = tmp_path / 'service'
    root.mkdir()
    config = {'service_root': str(root), 'configuration': {}}
    if damage == 'atlas_path':
        config['configuration']['atlas_path'] = str(tmp_path / 'atlas')
    elif damage == 'old-schema':
        with sqlite3.connect(root / 'operations.sqlite3') as db:
            db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
    else:
        (root / 'attempts').mkdir()
    (root / 'token').write_bytes(b'original')
    before = {p.name: p.read_bytes() if p.is_file() else None for p in root.iterdir()}
    from types import SimpleNamespace
    monkeypatch.setattr(launcher, 'subprocess', SimpleNamespace(
        Popen=lambda *a, **kw: pytest.fail('started service process'), DEVNULL=launcher.subprocess.DEVNULL))
    from knowledge_atlas import operations
    monkeypatch.setattr(operations.Dispatcher, 'start', lambda *a: pytest.fail('started worker'))
    with pytest.raises(AtlasError, match='unsupported'):
        if entry == 'launcher':
            launcher.ensure_service(config)
        elif entry == 'serve':
            create_app(ServiceContext(root, configuration=config['configuration'])).start()
        else:
            run_next(ServiceContext(root, configuration=config['configuration']))
    assert before == {p.name: p.read_bytes() if p.is_file() else None for p in root.iterdir()}


def test_concurrent_first_initialization_installs_one_complete_registry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from knowledge_atlas.operations import database
    service = ServiceContext(tmp_path / 'service')
    def submit_one(index):
        return submit(service, request(path=str(tmp_path / str(index)))).operation_id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = set(pool.map(submit_one, range(6)))
    with database(service) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert {r[0] for r in db.execute('SELECT operation_id FROM operations')} == ids


def test_queued_configuration_keeps_project(tmp_path):
    from knowledge_atlas.http_api import invoke
    from knowledge_atlas.operations import register_atlas
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.core.publication import Publications
    from knowledge_atlas.runtime import _assets
    from test_decisions import projection
    from test_packages import snapshot

    graph = {'url': 'http://127.0.0.1:8765', 'database': 'admin/inherited'}
    service = ServiceContext(tmp_path / 'service', configuration={'graph': graph},
                             handlers={'candidate.decide': blocking_handler})
    restarted_graph = {'url': 'http://127.0.0.1:8766', 'database': 'admin/changed'}
    restarted = ServiceContext(service.service_root, configuration={'graph': restarted_graph},
                               handlers=service.handlers)
    projects, operations = [], []
    profile = json.loads((_assets() / 'embedding-profile.json').read_text())
    release = tmp_path / 'release'
    for directory in ('first', 'second'):
        store = GitStore(tmp_path / directory)
        candidate = snapshot(store)
        atlas_id = store.read(candidate)['manifest']['package_id']
        register_atlas(service, atlas_id, store.base)
        pubs = Publications(store)
        content = review_acceptance(pubs, candidate, {'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture',
            'reason': 'Existing READY', 'accepted_at': '2026-11-13T01:00:00Z', 'base_publication': None}, profile)
        database = 'admin/atlas-' + uuid.UUID(atlas_id).hex
        ready = pubs.publish(content, lambda commit: {**projection(commit), 'database': database})
        projects.append((atlas_id, ready, database))
        operations.append(submit(service, ActionRequest('candidate.decide', atlas_id=atlas_id,
            request_id=str(uuid.uuid4()), expected_publication=ready, payload={'release': str(release)})))

    first = dispatch(restarted)
    try:
        wait_for(restarted, operations[0].operation_id, 'running')
        second = dispatch(restarted)
        second.join(10)
        assert second.exitcode == 0
        assert inspect_operation(restarted, operations[1].operation_id).state == 'queued'
        for atlas_id, ready, _ in projects:
            _, result = invoke(restarted, ActionRequest('atlas.status', atlas_id=atlas_id))
            assert result.publication == ready
    finally:
        release.touch()
        first.join(10)
        if first.is_alive():
            first.terminate()
            first.join(10)
    assert first.exitcode == 0
    second = dispatch(restarted)
    second.join(10)
    assert second.exitcode == 0
    for operation, (_, _, database) in zip(operations, projects):
        result = inspect_operation(restarted, operation.operation_id)
        assert result.state == 'succeeded', result.error
        assert result.result.data['frozen']['graph'] == {'url': graph['url'], 'database': database}


@pytest.mark.parametrize('drift', ['location', 'repository', 'graph', 'missing'])
def test_project_binding_change_fails_before_execution(tmp_path, drift):
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.operations import database, register_atlas
    from knowledge_atlas.worker import run_next
    from test_packages import snapshot

    service = ServiceContext(tmp_path / 'service', handlers={'candidate.decide': blocking_handler})
    store = GitStore(tmp_path / 'original')
    atlas_id = store.read(snapshot(store))['manifest']['package_id']
    register_atlas(service, atlas_id, store.base)
    release = tmp_path / 'release'
    release.touch()
    operation = submit(service, ActionRequest('candidate.decide', atlas_id=atlas_id,
        request_id=str(uuid.uuid4()), payload={'release': str(release)}))

    relocated = tmp_path / 'relocated'
    if drift in ('graph', 'missing'):
        with database(service) as db:
            row = db.execute('SELECT configuration FROM operations WHERE operation_id=?', (operation.operation_id,)).fetchone()
            frozen = json.loads(row[0])
            if drift == 'missing':
                del frozen['project_binding']
            else:
                frozen['project_binding']['graph_database'] = 'admin/reassigned'
            db.execute('UPDATE operations SET configuration=? WHERE operation_id=?',
                       (json.dumps(frozen), operation.operation_id))
    else:
        store.base.rename(relocated)
        if drift == 'location':
            register_atlas(service, atlas_id, relocated)
        else:
            snapshot(store)  # Reuse the path with a different repository UUID.

    run_next(service)
    result = inspect_operation(service, operation.operation_id)
    assert result.state == 'failed'
    assert result.error['code'] == ('graph_atlas_conflict' if drift == 'graph' else 'atlas_conflict')
    assert not (result.evidence / 'started').exists()
    with pytest.raises(AtlasError, match='atlas_conflict'):
        resume(service, operation.operation_id, str(uuid.uuid4()))
    assert inspect_operation(service, operation.operation_id).attempt == 1


def test_queued_destination_symlink_cannot_redirect_worker_lock(tmp_path):
    from knowledge_atlas.worker import run_next

    service = ServiceContext(tmp_path / 'service')
    destination = tmp_path / 'new'
    operation = submit(service, request(path=str(destination)))
    foreign_parent = tmp_path / 'replacement'
    foreign_parent.mkdir()
    destination.symlink_to(foreign_parent / 'new', target_is_directory=True)

    run_next(service)

    failed = inspect_operation(service, operation.operation_id)
    assert failed.state == 'failed'
    assert failed.error['code'] == 'atlas_conflict'
    assert list(foreign_parent.iterdir()) == []



def test_create_rejects_unavailable_registered_destination(tmp_path):
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project

    service = ServiceContext(tmp_path / 'service')
    destination = tmp_path / 'atlas'
    create_project(service, destination)
    original = list_projects(service)
    attempts = set((service.service_root / 'attempts').iterdir())
    destination.rename(tmp_path / 'moved')

    with pytest.raises(AtlasError) as failure:
        submit(service, request(path=str(destination)))

    assert (failure.value.code, failure.value.status) == ('atlas_conflict', 409)
    assert not destination.exists()
    assert list_projects(service) == original
    assert set((service.service_root / 'attempts').iterdir()) == attempts


@pytest.mark.parametrize(('registered_name', 'requested_name'), [
    pytest.param('atlas', 'atlas', id='exact'),
    pytest.param('caf\u00e9', 'cafe\u0301', id='unicode'),
])
@pytest.mark.parametrize('nested', [False, True], ids=['exact-location', 'child-location'])
def test_queued_create_rechecks_registered_destination_under_lock(tmp_path, monkeypatch,
                                                                  registered_name, requested_name, nested):
    from contextlib import contextmanager
    from knowledge_atlas import worker
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.operations import register_atlas
    from knowledge_atlas.project_registry import list_projects, repository_identity
    from test_packages import snapshot

    service = ServiceContext(tmp_path / 'service')
    alternate = tmp_path / requested_name
    destination = alternate/'child' if nested else alternate
    reserved = tmp_path / registered_name
    queued = submit(service, request(path=str(destination)))
    moved = tmp_path / 'moved'
    registrations = []
    aliases = []

    @contextmanager
    def register_before_worker_check(path):
        with AtlasLock(path):
            store = GitStore(reserved)
            identity = store.read(snapshot(store))['manifest']['package_id']
            registrations.append(register_atlas(service, identity, reserved))
            aliases.append(alternate.exists() and alternate.samefile(reserved))
            reserved.rename(moved)
            yield

    monkeypatch.setattr(worker, 'AtlasLock', register_before_worker_check)
    worker.run_next(service)

    result = inspect_operation(service, queued.operation_id)
    if aliases[0]:
        assert result.state == 'failed'
        assert result.error['code'] == ('invalid_location' if nested else 'atlas_conflict')
        assert not destination.exists()
        assert list_projects(service) == registrations
    else:
        assert result.state == 'succeeded', result.error
        assert result.result.data['atlas_id'] != registrations[0].atlas_id
        assert destination.exists() and not reserved.exists()
    assert repository_identity(moved) == registrations[0].atlas_id



@pytest.mark.parametrize(('registered_name', 'requested_name'), [
    pytest.param('atlas', 'ATLAS', id='case'),
    pytest.param('caf\u00e9', 'cafe\u0301', id='unicode'),
])
def test_create_reservation_respects_filesystem_name_equivalence(tmp_path, registered_name, requested_name):
    from knowledge_atlas.project_registry import list_projects
    from knowledge_atlas.worker import run_next
    from test_project_registry import create_project

    service = ServiceContext(tmp_path / 'service')
    reserved = tmp_path / registered_name
    alternate = tmp_path / requested_name
    identity = create_project(service, reserved)
    aliases_on_disk = alternate.exists() and alternate.samefile(reserved)
    reserved.rename(tmp_path / 'moved')
    assert not alternate.exists()
    surroundings = set(tmp_path.iterdir())

    if aliases_on_disk:
        with pytest.raises(AtlasError) as failure:
            submit(service, request(path=str(alternate)))
        assert (failure.value.code, failure.value.status) == ('atlas_conflict', 409)
        assert not alternate.exists()
        assert set(tmp_path.iterdir()) == surroundings
        assert [row.atlas_id for row in list_projects(service)] == [identity]
    else:
        operation = submit(service, request(path=str(alternate)))
        run_next(service)
        created = inspect_operation(service, operation.operation_id)
        assert created.state == 'succeeded', created.error
        assert created.result.data['atlas_id'] != identity
        assert alternate.exists() and not reserved.exists()



@pytest.mark.parametrize('boundary', ['registered', 'service', 'data-logs'])
def test_project_ancestor_respects_volume_name_equivalence(tmp_path, boundary):
    from knowledge_atlas.operations import atlas_path
    from test_project_registry import create_project

    service = ServiceContext(tmp_path/'service')
    if boundary == 'registered':
        root = tmp_path/'reserved'
        create_project(service, root)
    elif boundary == 'service':
        root = service.service_root
        root.mkdir(exist_ok=True)
    else:
        data = tmp_path/'data'
        service.configuration['project_data_root'] = str(data)
        root = data/'logs'
        root.mkdir(parents=True)
    alternate = root.with_name(root.name.upper())
    equivalent = alternate.exists() and alternate.samefile(root)
    if boundary == 'registered':
        root.rename(tmp_path/'moved')
    child = alternate/'new'/'child'
    for action in ('atlas.create', 'archive.restore'):
        req = request(action, path=str(child))
        if equivalent:
            with pytest.raises(AtlasError, match='invalid_location'):
                atlas_path(service, req)
        else:
            assert atlas_path(service, req) == child
    assert not child.exists()


@pytest.mark.parametrize('permission_change', ['before_admission', 'after_queueing'])
def test_unrelated_create_survives_inaccessible_registration(tmp_path, permission_change):
    from stat import S_IMODE
    from knowledge_atlas.project_registry import list_projects, repository_identity
    from knowledge_atlas.worker import run_next
    from test_project_registry import create_project

    service = ServiceContext(tmp_path / 'service')
    restricted = tmp_path / 'restricted'
    registered_path = restricted / 'old'
    original_uuid = create_project(service, registered_path)
    original_binding, = list_projects(service)
    destination = tmp_path / 'independent'
    creation = request(path=str(destination))
    if permission_change == 'after_queueing':
        operation = submit(service, creation)

    mode = S_IMODE(restricted.stat().st_mode)
    restricted.chmod(0)
    try:
        try:
            registered_path.stat()
        except PermissionError:
            pass
        else:
            pytest.skip('the test runtime can traverse a directory without search permissions')

        if permission_change == 'before_admission':
            operation = submit(service, creation)
        run_next(service)
        result = inspect_operation(service, operation.operation_id)
        assert result.state == 'succeeded', result.error
        assert result.result.data['atlas_id'] != original_uuid
        assert repository_identity(destination) == result.result.data['atlas_id']
    finally:
        restricted.chmod(mode)

    assert original_binding in list_projects(service)
    assert repository_identity(registered_path) == original_uuid


@pytest.mark.parametrize('loop_appears', ['before_admission', 'after_queueing'])
def test_unrelated_create_survives_symlink_loop_at_registration(tmp_path, loop_appears):
    import errno
    from knowledge_atlas.project_registry import list_projects, repository_identity
    from knowledge_atlas.worker import run_next
    from test_project_registry import create_project

    service = ServiceContext(tmp_path / 'service')
    parent = tmp_path / 'original'
    original_uuid = create_project(service, parent / 'old')
    original_binding, = list_projects(service)
    destination = tmp_path / 'independent'
    creation = request(path=str(destination))
    if loop_appears == 'after_queueing':
        operation = submit(service, creation)

    moved = tmp_path / 'moved'
    parent.rename(moved)
    parent.symlink_to(parent.name)
    with pytest.raises(OSError) as loop:
        original_binding.path.stat()
    assert loop.value.errno == errno.ELOOP

    if loop_appears == 'before_admission':
        operation = submit(service, creation)
    run_next(service)
    result = inspect_operation(service, operation.operation_id)
    assert result.state == 'succeeded', result.error
    assert result.result.data['atlas_id'] == repository_identity(destination)
    assert result.result.data['atlas_id'] != original_uuid
    assert original_binding in list_projects(service)
    assert repository_identity(moved / 'old') == original_uuid


def interrupt_configuration_write(path):
    import subprocess
    import sys
    script = """import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=1')
db.execute("UPDATE operations SET configuration=replace(configuration,'reviewed','unproven')")
os._exit(0)
"""
    subprocess.run([sys.executable, '-c', script, str(path)], check=True)


@pytest.mark.parametrize('valid', [True, False])
def test_hot_transaction_validates_without_rewriting_before_recovery(tmp_path, valid):
    from knowledge_atlas.operations import check_service_storage, database
    service = ServiceContext(tmp_path / 'service', configuration={'notes': 'reviewed' * 20000})
    operation = submit(service, request(path=str(tmp_path / 'atlas')))
    path = service.service_root / 'operations.sqlite3'
    with database(service) as db:
        original = db.execute('SELECT configuration FROM operations').fetchone()[0]
        if not valid:
            db.execute('DROP TABLE resumes')
    interrupt_configuration_write(path)
    journal = path.with_name(path.name + '-journal')
    before = path.read_bytes(), journal.read_bytes()
    if not valid:
        with pytest.raises(AtlasError, match='unsupported'):
            with database(service):
                pytest.fail('opened unsupported hot database')
        assert before == (path.read_bytes(), journal.read_bytes())
        return
    check_service_storage(service)
    assert before == (path.read_bytes(), journal.read_bytes())
    with database(service) as db:
        assert db.execute('SELECT operation_id,configuration FROM operations').fetchone()[:] == (operation.operation_id, original)
    assert not journal.exists()


def test_concurrent_first_native_launch_attaches_to_one_service(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from knowledge_atlas.client import AtlasClient
    from knowledge_atlas.launcher import ensure_service
    root = tmp_path / 'service'
    config = {'service_root': str(root)}
    try:
        with ThreadPoolExecutor(max_workers=3) as starts:
            descriptors = list(starts.map(ensure_service, [config] * 3))
        assert len({(d['pid'], d['endpoint']) for d in descriptors}) == 1
        assert AtlasClient(root).request('/v1/status')['version']
    finally:
        if (root / 'discovery.json').is_file():
            AtlasClient(root).stop()


def test_interrupted_current_sqlite_initialization_retries_without_exposing_partial_state(tmp_path):
    import subprocess
    import sys
    from knowledge_atlas.operations import database
    root = tmp_path / 'service'
    script = """import os, sys
from pathlib import Path
from knowledge_atlas import storage
from knowledge_atlas.contracts import ServiceContext
from knowledge_atlas.operations import database
storage.os.link = lambda *args: os._exit(17)
with database(ServiceContext(Path(sys.argv[1]))):
    raise AssertionError('exposed an uninstalled database')
"""
    assert subprocess.run([sys.executable, '-c', script, str(root)]).returncode == 17
    assert not (root / 'operations.sqlite3').exists()
    with database(ServiceContext(root)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0

def test_preflight_keeps_another_process_out_of_an_active_write_transaction(tmp_path):
    import os
    import subprocess
    import sys
    from knowledge_atlas.operations import check_service_storage, database
    service = ServiceContext(tmp_path / 'service')
    path = service.service_root / 'operations.sqlite3'
    probe = """import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=0) as other:
    try:
        other.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as error:
        assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
        print('busy')
    else:
        print('available')
"""
    def reservation():
        return subprocess.check_output([sys.executable, '-c', probe, str(path)], text=True).strip()
    with database(service) as db:
        db.execute('BEGIN IMMEDIATE')
        os.utime(path)  # Force inspection even if the preceding open cached a header.
        assert reservation() == 'busy'
        check_service_storage(service)
        assert reservation() == 'busy'
    assert reservation() == 'available'


@pytest.mark.parametrize('direct', [True, False])
def test_native_service_root_refuses_literal_symlink(tmp_path, direct):
    from knowledge_atlas.launcher import service_context
    from knowledge_atlas.operations import initialize_service_storage
    target = tmp_path / 'empty'
    target.mkdir()
    link = tmp_path / 'service'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(AtlasError, match='unsupported'):
        ctx = ServiceContext(link) if direct else service_context({'service_root': str(link)})
        initialize_service_storage(ctx)
    assert link.is_symlink() and not list(target.iterdir())


def test_registry_preflight_admits_reads_while_current_writer_progresses(tmp_path):
    import subprocess
    import sys
    from knowledge_atlas.operations import check_service_storage
    from knowledge_atlas.project_registry import list_projects
    from test_project_registry import create_project
    service = ServiceContext(tmp_path / 'service')
    project = create_project(service, tmp_path / 'project')
    path = service.service_root / 'operations.sqlite3'
    stop = tmp_path / 'stop-writer'
    script = """import sqlite3, sys, time
from pathlib import Path
db = sqlite3.connect(sys.argv[1], isolation_level=None)
deadline = time.monotonic() + 20
count = 0
while time.monotonic() < deadline and not Path(sys.argv[3]).exists():
    count += 1
    db.execute('UPDATE atlases SET git_url=? WHERE atlas_id=?',
               (f'https://example.invalid/project?revision={count}', sys.argv[2]))
    if count == 1:
        print('writing', flush=True)
db.close()
"""
    proc = subprocess.Popen([sys.executable, '-c', script, str(path), project, str(stop)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'writing'
        observed = []
        for _ in range(4):
            check_service_storage(service)
            binding, = list_projects(service)
            assert binding.atlas_id == project
            observed.append(int(binding.git_url.rsplit('=', 1)[1]))
        assert observed[-1] > observed[0] and proc.poll() is None
    finally:
        stop.touch()
        assert proc.wait(timeout=10) == 0
        proc.stdout.close()


@pytest.mark.parametrize('selection', [
    {'atlas_id': '92000000-0000-4000-8000-000000000001'}, {'expected_publication': '1'*40},
    {'publication': '2'*40}, {'as_known_at': '2026-11-10T10:00:00Z'}, {'valid_at': '2026-11-10T10:00:00Z'},
])
def test_resume_refuses_changed_selectors_on_exact_retry(tmp_path, selection):
    from knowledge_atlas.api_models import parse_request
    from knowledge_atlas.http_api import invoke
    from knowledge_atlas.operations import database
    service = ServiceContext(tmp_path / 'service')
    original = submit(service, request(path=str(tmp_path / 'project')))
    with database(service) as db:
        db.execute("UPDATE operations SET state='failed' WHERE operation_id=?", (original.operation_id,))
    body = {'request_id': str(uuid.uuid4()), 'payload': {'operation_id': original.operation_id}}
    status, resumed = invoke(service, parse_request('operation.resume', body))
    assert status == 202 and resumed.attempt == 2
    with pytest.raises(AtlasError, match='invalid_request|request_conflict'):
        invoke(service, parse_request('operation.resume', {**body, **selection}))
    assert invoke(service, parse_request('operation.resume', body))[1] == resumed
