"""The project registry owns selection and the graph database for each UUID."""
import json
import sqlite3
import uuid

import pytest

from knowledge_atlas.api_models import parse_request
from knowledge_atlas.contracts import ActionContext, ActionRequest, AtlasError, ServiceContext
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.http_api import invoke
from knowledge_atlas.operations import inspect_operation, register_atlas, submit
from knowledge_atlas.runtime import _assets
from knowledge_atlas.worker import run_next
from test_decisions import projection


def create_project(service, path):
    response = submit(service, ActionRequest('atlas.create', request_id=str(uuid.uuid4()),
                                            payload={'path': str(path)}))
    run_next(service)
    record = inspect_operation(service, response.operation_id)
    assert record.state == 'succeeded', record.error
    return record.result.data['atlas_id']


def test_list_without_atlas_or_payload(tmp_path):
    service = ServiceContext(tmp_path / 'service')
    atlas_id = create_project(service, tmp_path / 'field notes')

    status, result = invoke(service, parse_request('atlas.list', {}))

    assert status == 200
    assert result.data['projects'] == [
        {'atlas_id': atlas_id, 'name': 'field notes', 'publication': None, 'status': 'unpublished'}
    ]


def test_open_uuid_never_falls_back_to_path(tmp_path):
    path = tmp_path / 'legacy'
    service = ServiceContext(tmp_path / 'service', configuration={})
    atlas_id = create_project(service, path)
    for selection, payload, expected in [
        (str(uuid.uuid4()), {}, ('atlas_not_found', 404)),
        (None, {}, ('invalid_request', 400)),
        ('malformed', {}, ('invalid_request', 400)),
        (atlas_id, {'path': str(path)}, ('invalid_request', 400)),
    ]:
        with pytest.raises(AtlasError) as failure:
            invoke(service, ActionRequest('atlas.open', atlas_id=selection, payload=payload))
        assert (failure.value.code, failure.value.status) == expected
    assert invoke(service, ActionRequest('atlas.open', atlas_id=atlas_id))[1].data['atlas_id'] == atlas_id


def test_repository_uuid_mismatch(tmp_path):
    service = ServiceContext(tmp_path / 'service')
    path = tmp_path / 'atlas'
    atlas_id = create_project(service, path)
    path.rename(tmp_path / 'original')
    other = ServiceContext(tmp_path / 'other-service')
    assert create_project(other, path) != atlas_id

    for action in ('atlas.status', 'atlas.open'):
        with pytest.raises(AtlasError) as failure:
            invoke(service, ActionRequest(action, atlas_id=atlas_id))
        assert (failure.value.code, failure.value.status) == ('atlas_conflict', 409)
    with pytest.raises(AtlasError, match='atlas_conflict'):
        submit(service, ActionRequest('sources.submit', atlas_id=atlas_id, request_id=str(uuid.uuid4())))
    assert invoke(service, ActionRequest('atlas.list'))[1].data['projects'] == [
        {'atlas_id': atlas_id, 'name': 'atlas', 'publication': None, 'status': 'unavailable'}
    ]


def test_distinct_graph_bindings_survive_restart(tmp_path):
    shared = {'url': 'http://127.0.0.1:8765', 'database': 'admin/inherited'}
    service = ServiceContext(tmp_path / 'service', configuration={'graph': shared})
    first, second = (create_project(service, tmp_path / area / 'notes') for area in ('one', 'two'))
    restarted = ServiceContext(service.service_root, configuration={'graph': {**shared, 'database': 'admin/changed'}})

    for atlas_id, area in ((first, 'one'), (second, 'two')):
        request = ActionRequest('atlas.status', atlas_id=atlas_id)
        expected = 'admin/atlas-' + uuid.UUID(atlas_id).hex
        assert ActionContext.for_read(service, request).configuration['graph']['database'] == expected
        assert ActionContext.for_read(restarted, request).configuration['graph']['database'] == expected
        assert invoke(restarted, ActionRequest('atlas.open', atlas_id=atlas_id))[1].data['atlas_id'] == atlas_id
        assert ActionContext.for_read(restarted, request).atlas_path == tmp_path / area / 'notes'
    assert shared['database'] == 'admin/inherited'


def test_registration_requires_the_repository_uuid(tmp_path):
    path = tmp_path / 'atlas'
    original = create_project(ServiceContext(tmp_path / 'fixture-service'), path)
    service = ServiceContext(tmp_path / 'service')
    with pytest.raises(AtlasError, match='atlas_conflict'):
        register_atlas(service, str(uuid.uuid4()), path)
    register_atlas(service, original, path)
    assert invoke(service, ActionRequest('atlas.open', atlas_id=original))[1].data['atlas_id'] == original


def retained_publication(path, vector_artifacts=None, *, database=None, service_root=None):
    identity = create_project(ServiceContext(service_root or path.parent / 'fixture-service'), path)
    store = GitStore(path)
    pubs = Publications(store)
    profile = json.loads((_assets() / 'embedding-profile.json').read_text())
    content = pubs.review(git(store.root, 'rev-parse', 'HEAD'), {
        'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture', 'reason': 'Retained publication fixture',
        'accepted_at': '2026-11-13T01:00:00Z', 'base_publication': None,
    }, profile)
    name = database or 'admin/atlas-' + uuid.UUID(identity).hex
    ready = pubs.publish(content, lambda oid: {**projection(oid), 'database': name,
                                               'vector_artifacts': vector_artifacts or {}})
    return identity, store, ready


def vector_fixture(cache, text='retained text'):
    from knowledge_atlas.core.embedding_provider import EmbeddingProvider

    provider = EmbeddingProvider(_assets() / 'embedding-profile.json', cache, 'offline')
    provider.request = lambda path, body=None: provider.profile['profile']['endpoint'] if path == '/info' else [[1.] * 768]
    body = {'input': [text], 'role': 'document', 'model': provider.profile['model_namespace'], 'dimensions': 256}
    provider.embed(body)
    return provider, body


def test_rebuild_seeds_archive_vectors_before_native_replay(tmp_path, monkeypatch):
    from knowledge_atlas.core import archive
    from knowledge_atlas.native_pipeline import project_native

    source, body = vector_fixture(tmp_path / 'source-cache')
    expected = source.embed(body)
    with source.connect() as db:
        artifact = json.loads(db.execute('SELECT artifact FROM vectors').fetchone()[0])
    identity, store, _ = retained_publication(tmp_path / 'published', {artifact['input_sha256']: artifact})
    old = store.base / 'vectors/vectors.sqlite3'
    old.parent.mkdir()
    old.write_bytes(b'unmarked unused old cache')
    cache = tmp_path / 'service/vectors'
    def replay(restored, builder):
        from knowledge_atlas.core.embedding_provider import EmbeddingProvider
        assert builder.cache == cache
        shared = EmbeddingProvider(_assets() / 'embedding-profile.json', builder.cache, 'offline')
        assert shared.embed(body) == expected
        return {'ready': 'retained'}
    monkeypatch.setattr(archive, 'rebuild_projections', replay)
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    payload = {'configuration': {'graph': {'url': 'http://127.0.0.1:9403', 'database': 'admin/atlas-' + uuid.UUID(identity).hex}},
               'vector_cache': str(cache), 'runtime_root': str(tmp_path / 'runtime'), 'evidence': str(evidence),
               'request': {'action': 'publication.rebuild', 'payload': {}}}
    assert project_native(payload, store).data == {'ready': 'retained'}
    assert old.read_bytes() == b'unmarked unused old cache'



@pytest.mark.parametrize('damage', ['zero-version', 'missing-schema', 'missing-binding', 'ambiguous-owner'])
def test_invalid_registry_registration_is_never_completed_by_a_read(tmp_path, damage):
    from knowledge_atlas.operations import database
    from knowledge_atlas.project_registry import list_projects
    service = ServiceContext(tmp_path / 'service')
    identity, store, ready = retained_publication(tmp_path / 'atlas')
    register_atlas(service, identity, store.base)
    path = service.service_root / 'operations.sqlite3'
    with sqlite3.connect(path) as db:
        if damage == 'zero-version':
            db.execute('PRAGMA user_version=0')
        elif damage == 'missing-schema':
            db.execute('DROP TABLE resumes')
        elif damage == 'missing-binding':
            db.execute('ALTER TABLE atlases RENAME TO original')
            db.execute('CREATE TABLE atlases AS SELECT atlas_id,path,NULL AS graph_database,git_url FROM original')
            db.execute('DROP TABLE original')
        else:
            db.execute('INSERT INTO projection_owners VALUES (?,?,?)',
                       ('http://127.0.0.1:9403', 'admin/atlas-' + uuid.UUID(identity).hex, str(uuid.uuid4())))
    original = path.read_bytes()
    refs = git(store.root, 'show-ref')
    with pytest.raises(AtlasError, match='unsupported'):
        list_projects(service)
    assert path.read_bytes() == original
    assert git(store.root, 'show-ref') == refs and Publications(store).ready() == ready


def test_listing_fresh_and_current_registry_is_read_only(tmp_path):
    from knowledge_atlas.project_registry import list_projects
    service = ServiceContext(tmp_path / 'service')
    assert list_projects(service) == []
    assert not service.service_root.exists()
    identity = create_project(service, tmp_path / 'one')
    path = service.service_root / 'operations.sqlite3'
    original = path.read_bytes()
    assert [p.atlas_id for p in list_projects(service)] == [identity]
    assert original == path.read_bytes()


def test_bridge_first_service_start_preserves_verified_vectors(tmp_path):
    from knowledge_atlas.operations import database
    service = ServiceContext(tmp_path / 'service')
    provider, body = vector_fixture(service.service_root / 'vectors')
    original = (provider.cache / 'vectors.sqlite3').read_bytes()
    with database(service) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    with provider.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    assert (provider.cache / 'vectors.sqlite3').read_bytes() == original
    provider.request = lambda *_: pytest.fail('restarted bridge performed inference')
    assert provider.embed(body)['data'][0]['embedding'] == [1.] * 256


def test_rejected_cache_merge_preserves_source_and_absent_destination(tmp_path):
    from knowledge_atlas.core.embedding_provider import merge_verified_cache
    provider, _ = vector_fixture(tmp_path / 'source')
    with provider.connect() as db:
        db.execute('DELETE FROM vector_integrity')
    original = (provider.cache / 'vectors.sqlite3').read_bytes()
    destination = tmp_path / 'destination'
    with pytest.raises(ValueError, match='vector'):
        merge_verified_cache(provider.cache, destination)
    assert original == (provider.cache / 'vectors.sqlite3').read_bytes()
    assert not destination.exists()


def test_unmarked_wal_registry_leaves_no_sqlite_sidecars(tmp_path):
    from contextlib import closing
    from knowledge_atlas.operations import check_service_storage
    service = ServiceContext(tmp_path / 'service')
    service.service_root.mkdir()
    path = service.service_root / 'operations.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY)')
    before = {p.name: p.read_bytes() for p in service.service_root.iterdir()}
    with pytest.raises(AtlasError, match='unsupported'):
        check_service_storage(service)
    assert before == {p.name: p.read_bytes() for p in service.service_root.iterdir()}


@pytest.mark.parametrize('access', ['read', 'write', 'write-new-invalid-schema', 'write-replaced-file'])
def test_registry_validation_during_admitted_handle_recovery(tmp_path, monkeypatch, access):
    from knowledge_atlas import storage
    from knowledge_atlas.operations import check_service_storage, database
    from test_operations import interrupt_configuration_write
    service = ServiceContext(tmp_path / 'service', configuration={'notes': 'reviewed' * 20000})
    submit(service, ActionRequest('atlas.create', request_id=str(uuid.uuid4()), payload={'path': str(tmp_path / 'p')}))
    path = service.service_root / 'operations.sqlite3'
    journal = path.with_name(path.name + '-journal')
    verify_schema = storage.verify_schema
    with database(service) as admitted:
        interrupt_configuration_write(path)
        assert journal.exists()
        validated = []
        def interleave(snapshot, schema):
            verify_schema(snapshot, schema)
            validated.append(snapshot.execute('SELECT count(*) FROM operations').fetchone()[0])
            if len(validated) == 1:
                assert admitted.execute('SELECT count(*) FROM operations').fetchone()[0] == 1
                assert not journal.exists()
                if access == 'write-new-invalid-schema':
                    admitted.execute('DROP TABLE resumes')
                    admitted.commit()
                elif access == 'write-replaced-file':
                    path.rename(tmp_path / 'displaced.sqlite3')
                    path.write_bytes(b'unrelated replacement')
        monkeypatch.setattr(storage, 'verify_schema', interleave)
        if access == 'read':
            check_service_storage(service)
        elif access == 'write-new-invalid-schema':
            with pytest.raises(AtlasError, match='unsupported.*schema'):
                with database(service):
                    pytest.fail('opened changed unsupported registry')
        elif access == 'write-replaced-file':
            with pytest.raises(AtlasError, match='replaced'):
                with database(service):
                    pytest.fail('opened replacement')
            assert path.read_bytes() == b'unrelated replacement'
        else:
            with database(service) as current:
                assert current.execute('SELECT count(*) FROM operations').fetchone()[0] == 1
        assert validated and not journal.exists()

def test_preflight_rechecks_cached_header_after_in_place_journal_mode_change(tmp_path):
    from contextlib import closing
    from knowledge_atlas.operations import check_service_storage, initialize_service_storage
    service = ServiceContext(tmp_path / 'service')
    initialize_service_storage(service)
    check_service_storage(service)
    path = service.service_root / 'operations.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    before = {p.name: p.read_bytes() for p in service.service_root.iterdir()}
    with pytest.raises(AtlasError, match='unsupported.*format'):
        check_service_storage(service)
    assert before == {p.name: p.read_bytes() for p in service.service_root.iterdir()}


def test_preflight_rejects_wal_transition_after_physical_header_read(tmp_path, monkeypatch):
    from contextlib import closing
    from knowledge_atlas import storage
    from knowledge_atlas.operations import check_service_storage, initialize_service_storage
    service = ServiceContext(tmp_path / 'service')
    initialize_service_storage(service)
    path = service.service_root / 'operations.sqlite3'
    probe = storage._probe
    wal_contents = None

    def switch_after_header(command, *args):
        nonlocal wal_contents
        reply = probe(command, *args)
        if command == 'header' and args[0] == path and wal_contents is None:
            with closing(sqlite3.connect(path)) as db:
                assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
            wal_contents = path.read_bytes()
            assert list(service.service_root.iterdir()) == [path]
        return reply

    monkeypatch.setattr(storage, '_probe', switch_after_header)
    with pytest.raises(AtlasError, match='unsupported.*format'):
        check_service_storage(service)
    assert wal_contents is not None and path.read_bytes() == wal_contents
    assert list(service.service_root.iterdir()) == [path]


@pytest.mark.parametrize(('installation', 'entry'), [
    (installation, entry)
    for installation in ('missing', 'broken')
    for entry in ('fresh-service', 'current-service', 'fresh-cache')
] + [('broken-header', entry) for entry in ('fresh-service', 'current-service', 'fresh-cache')])
def test_unusable_inspection_helper_preserves_supplied_storage(tmp_path, installation, entry):
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    import knowledge_atlas
    from knowledge_atlas.operations import initialize_service_storage
    root = tmp_path / 'supplied'
    if entry == 'current-service':
        initialize_service_storage(ServiceContext(root))
    original = {p.name: p.read_bytes() for p in root.iterdir()} if root.exists() else None
    isolated = tmp_path / 'installation/src'
    package = isolated / 'knowledge_atlas'
    shutil.copytree(Path(knowledge_atlas.__file__).parent, package,
                    ignore=shutil.ignore_patterns('__pycache__', '_sqlite_probe.py'))
    if installation == 'broken':
        (package / '_sqlite_probe.py').write_text('raise SystemExit(2)\n')
    elif installation == 'broken-header':
        from knowledge_atlas.storage import _PROBE_PROTOCOL
        (package / '_sqlite_probe.py').write_text(
            f"import sys\nif sys.argv[1:] == ['ready']:\n    sys.stdout.buffer.write({_PROBE_PROTOCOL!r})\n"
            "else:\n    raise SystemExit(2)\n")
    script = """import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from knowledge_atlas import launcher, storage
from knowledge_atlas.core.embedding_provider import initialize_cache
assert Path(storage.__file__).resolve().parent.parent == Path(sys.argv[1])
def reject_launch(*args, **kwargs):
    raise AssertionError('service process launched')
launcher.subprocess = SimpleNamespace(Popen=reject_launch, DEVNULL=launcher.subprocess.DEVNULL)
try:
    if sys.argv[3] == 'fresh-cache':
        initialize_cache(Path(sys.argv[2]))
    else:
        launcher.ensure_service({'service_root': sys.argv[2]})
except RuntimeError as error:
    assert 'SQLite inspection helper' in str(error), str(error)
else:
    raise AssertionError('incomplete installation was admitted')
"""
    result = subprocess.run([sys.executable, '-B', '-c', script, str(isolated), str(root), entry],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    after = {p.name: p.read_bytes() for p in root.iterdir()} if root.exists() else None
    assert after == original
