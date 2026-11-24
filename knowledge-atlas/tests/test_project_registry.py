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
    service = ServiceContext(tmp_path / 'service', configuration={'atlas_path': str(path)})
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


def test_registry_preserves_verified_legacy_database(tmp_path):
    path = tmp_path / 'legacy'
    atlas_id = create_project(ServiceContext(tmp_path / 'fixture-service'), path)
    store = GitStore(path)
    pubs = Publications(store)
    profile = json.loads((_assets() / 'embedding-profile.json').read_text())
    content = pubs.review(git(store.root, 'rev-parse', 'HEAD'), {
        'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture', 'reason': 'Legacy publication',
        'accepted_at': '2026-11-13T01:00:00Z', 'base_publication': None,
    }, profile)
    ready = pubs.publish(content, lambda commit: {**projection(commit), 'database': 'admin/retained', 'vector_artifacts': {}})
    git(store.root, 'config', '--local', 'remote.origin.url', 'ssh://git.example/legacy.git')
    service = ServiceContext(tmp_path / 'service', configuration={
        'graph': {'url': 'http://127.0.0.1:8765', 'database': 'admin/inherited'},
    })
    service.service_root.mkdir()
    with sqlite3.connect(service.service_root / 'operations.sqlite3') as database:
        database.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
        database.execute('INSERT INTO atlases VALUES (?, ?)', (atlas_id, str(path)))

    from knowledge_atlas.project_registry import list_projects

    assert list_projects(service)[0].graph_database == 'admin/retained'
    request = ActionRequest('atlas.status', atlas_id=atlas_id)
    resolved = ActionContext.for_read(service, request)
    assert resolved.configuration['graph']['database'] == 'admin/retained'
    assert resolved.project_binding.git_url == 'ssh://git.example/legacy.git'
    assert invoke(service, ActionRequest('atlas.open', atlas_id=atlas_id))[1].publication == ready
    assert register_atlas(ServiceContext(service.service_root), atlas_id, path) == resolved.project_binding


def test_registration_requires_the_repository_uuid(tmp_path):
    path = tmp_path / 'atlas'
    original = create_project(ServiceContext(tmp_path / 'fixture-service'), path)
    service = ServiceContext(tmp_path / 'service')
    with pytest.raises(AtlasError, match='atlas_conflict'):
        register_atlas(service, str(uuid.uuid4()), path)
    register_atlas(service, original, path)
    assert invoke(service, ActionRequest('atlas.open', atlas_id=original))[1].data['atlas_id'] == original


def legacy_publication(path, vector_artifacts=None):
    identity = create_project(ServiceContext(path.parent / 'fixture-service'), path)
    store = GitStore(path)
    pubs = Publications(store)
    profile = json.loads((_assets() / 'embedding-profile.json').read_text())
    content = pubs.review(git(store.root, 'rev-parse', 'HEAD'), {
        'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture', 'reason': 'Legacy',
        'accepted_at': '2026-11-13T01:00:00Z', 'base_publication': None,
    }, profile)
    ready = pubs.publish(content, lambda oid: {**projection(oid), 'database': 'admin/atlas',
                                               'vector_artifacts': vector_artifacts or {}})
    return identity, store, ready


def test_adopt_existing_uuid_and_database_once(tmp_path):
    from knowledge_atlas.project_registry import adopt_existing_atlas, list_projects

    path = tmp_path / 'atlas'
    identity, store, ready = legacy_publication(path)
    refs = git(store.root, 'show-ref')
    service = ServiceContext(tmp_path / 'service')
    for _ in range(2):
        binding = adopt_existing_atlas(service, path, 'admin/atlas')
        assert (binding.atlas_id, binding.path, binding.graph_database) == (identity, path, 'admin/atlas')
        assert list_projects(service) == [binding]
        assert Publications(store).ready() == ready
        assert git(store.root, 'show-ref') == refs


def test_adoption_failure_preserves_original(tmp_path):
    from knowledge_atlas.project_registry import adopt_existing_atlas, list_projects

    path = tmp_path / 'atlas'
    identity, store, ready = legacy_publication(path)
    refs = git(store.root, 'show-ref')
    service = ServiceContext(tmp_path / 'service')
    with pytest.raises(AtlasError, match='graph_atlas_conflict'):
        adopt_existing_atlas(service, path, 'admin/other')
    assert list_projects(service) == []
    assert git(store.root, 'show-ref') == refs
    assert Publications(store).ready() == ready
    assert adopt_existing_atlas(service, path, 'admin/atlas').atlas_id == identity


def vector_fixture(cache, text='retained text'):
    from knowledge_atlas.core.embedding_provider import EmbeddingProvider

    provider = EmbeddingProvider(_assets() / 'embedding-profile.json', cache, 'offline')
    provider.request = lambda path, body=None: provider.profile['profile']['endpoint'] if path == '/info' else [[1.] * 768]
    body = {'input': [text], 'role': 'document', 'model': provider.profile['model_namespace'], 'dimensions': 256}
    provider.embed(body)
    return provider, body


def test_adoption_copies_verified_cache_before_registration(tmp_path):
    from knowledge_atlas.core.embedding_provider import EmbeddingProvider
    from knowledge_atlas.project_registry import adopt_existing_atlas, list_projects

    path = tmp_path / 'atlas'
    identity, store, ready = legacy_publication(path)
    source, body = vector_fixture(path / 'vectors')
    with source.connect() as db:
        key, digest = db.execute('SELECT key, sha256 FROM vector_integrity').fetchone()
        db.execute("UPDATE vector_integrity SET sha256='damaged'")
    service = ServiceContext(tmp_path / 'service')
    with pytest.raises(AtlasError, match='integrity'):
        adopt_existing_atlas(service, path, 'admin/atlas')
    assert list_projects(service) == []
    assert Publications(store).ready() == ready
    with source.connect() as db:
        db.execute('UPDATE vector_integrity SET sha256=? WHERE key=?', (digest, key))
    assert adopt_existing_atlas(service, path, 'admin/atlas').atlas_id == identity
    shared = EmbeddingProvider(_assets() / 'embedding-profile.json', service.service_root / 'vectors', 'offline')
    assert shared.embed(body)['data'] == source.embed(body)['data']
    assert shared.stats['inference_calls'] == 0


def test_rebuild_seeds_archive_vectors_before_native_replay(tmp_path, monkeypatch):
    from knowledge_atlas.core import archive
    from knowledge_atlas.native_pipeline import project_native

    store = GitStore(tmp_path / 'restored')
    source, body = vector_fixture(store.base / 'vectors')
    expected = source.embed(body)
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
    payload = {'configuration': {'graph': {'url': 'http://127.0.0.1:9403', 'database': 'admin/restored'}},
               'vector_cache': str(cache), 'runtime_root': str(tmp_path / 'runtime'), 'evidence': str(evidence),
               'request': {'action': 'publication.rebuild', 'payload': {}}}
    assert project_native(payload, store).data == {'ready': 'retained'}


def test_legacy_bridge_cache_without_project_keeps_verified_vectors(tmp_path):
    from knowledge_atlas.core.embedding_provider import EmbeddingProvider
    from knowledge_atlas.project_registry import adopt_existing_atlas, list_projects

    service = ServiceContext(tmp_path / 'service')
    path = tmp_path / 'atlas'
    source, body = vector_fixture(path / 'vectors')
    assert adopt_existing_atlas(service, path, 'admin/atlas') is None
    assert list_projects(service) == []
    target = EmbeddingProvider(_assets() / 'embedding-profile.json', service.service_root / 'vectors', 'offline')
    assert target.embed(body) == source.embed(body)


def test_legacy_native_registry_migrates_cache_with_binding(tmp_path):
    from knowledge_atlas.core.embedding_provider import EmbeddingProvider
    from knowledge_atlas.project_registry import resolve_project

    path = tmp_path / 'legacy'
    identity, store, ready = legacy_publication(path)
    source, body = vector_fixture(path / 'vectors')
    service = ServiceContext(tmp_path / 'service')
    service.service_root.mkdir()
    with sqlite3.connect(service.service_root / 'operations.sqlite3') as db:
        db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
        db.execute('INSERT INTO atlases VALUES (?,?)', (identity, str(path)))
    assert resolve_project(service, identity).graph_database == 'admin/atlas'
    target = EmbeddingProvider(_assets() / 'embedding-profile.json', service.service_root / 'vectors', 'offline')
    assert target.embed(body) == source.embed(body)
    assert Publications(store).ready() == ready


def test_adoption_rejects_another_unmigrated_owner(tmp_path):
    from knowledge_atlas.project_registry import adopt_existing_atlas

    first, _, _ = legacy_publication(tmp_path / 'retained')
    _, store, ready = legacy_publication(tmp_path / 'legacy')
    service = ServiceContext(tmp_path / 'service')
    service.service_root.mkdir()
    with sqlite3.connect(service.service_root / 'operations.sqlite3') as db:
        db.execute('CREATE TABLE atlases (atlas_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)')
        db.execute('INSERT INTO atlases VALUES (?,?)', (first, str(tmp_path / 'retained')))
    with pytest.raises(AtlasError, match='graph_atlas_conflict'):
        adopt_existing_atlas(service, store.base, 'admin/atlas')
    assert Publications(store).ready() == ready


def test_conflicting_git_vector_rolls_back_adoption_cache(tmp_path):
    from knowledge_atlas.core.embedding_provider import cache_artifact
    from knowledge_atlas.project_registry import adopt_existing_atlas, list_projects

    fixture, _ = vector_fixture(tmp_path / 'fixture-vectors')
    with fixture.connect() as db:
        key, value = db.execute('SELECT key,artifact FROM vectors').fetchone()
    canonical = {**json.loads(value), 'raw768': [2.] * 768}
    identity, store, ready = legacy_publication(tmp_path / 'atlas', {canonical['input_sha256']: canonical})
    source, _ = vector_fixture(store.base / 'vectors')
    service = ServiceContext(tmp_path / 'service')
    shared, _ = vector_fixture(service.service_root / 'vectors', 'another project')
    rows = 'SELECT key,artifact,sha256 FROM vectors JOIN vector_integrity USING(key) ORDER BY key'
    with shared.connect() as db:
        previous = db.execute(rows).fetchall()

    with pytest.raises(AtlasError, match='conflicting retained vector artifacts'):
        adopt_existing_atlas(service, store.base, 'admin/atlas')
    with shared.connect() as db:
        assert db.execute(rows).fetchall() == previous
    assert not list_projects(service)
    assert Publications(store).ready() == ready

    with source.connect() as db:
        db.execute('DELETE FROM vectors WHERE key=?', (key,))
        cache_artifact(db, key, canonical)
    assert adopt_existing_atlas(service, store.base, 'admin/atlas').atlas_id == identity
    with shared.connect() as db:
        assert json.loads(db.execute('SELECT artifact FROM vectors WHERE key=?', (key,)).fetchone()[0]) == canonical
        assert previous[0] in db.execute(rows).fetchall()
