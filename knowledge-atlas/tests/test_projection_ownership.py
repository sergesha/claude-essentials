"""Per-project graph ownership is required before native projection writes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from knowledge_atlas import native_pipeline
from acceptance_fixture import review_acceptance
from knowledge_atlas.contracts import ActionContext, ActionRequest, AtlasError, ServiceContext
from knowledge_atlas.core.git_store import GitStore
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.native_projection import NativeProjection
from knowledge_atlas.operations import database, register_atlas
from knowledge_atlas.project_registry import project_configuration
from knowledge_atlas.runtime import _assets
from test_decisions import projection
from test_packages import snapshot


def setup_atlas(path, service, graph):
    store = GitStore(path)
    candidate = snapshot(store)
    manifest = store.read(candidate)['manifest']
    binding = register_atlas(service, manifest['package_id'], path)
    pub = Publications(store)
    profile = json.loads((_assets()/'embedding-profile.json').read_text())
    accepted = review_acceptance(pub, candidate, {'decision':'accept','kind':'synthetic','actor':'Fixture',
        'reason':'Ownership test','accepted_at':'2026-11-13T01:00:00Z','base_publication':None}, profile)
    request = ActionRequest('publication.build', atlas_id=binding.atlas_id, payload={'content':accepted})
    configuration = project_configuration(replace(service, configuration={'graph':graph}), binding)
    ctx = ActionContext(service, request, path, path/'evidence', configuration, project_binding=binding)
    return ctx, pub, accepted


def test_existing_publication_prevents_another_atlas_from_entering_native_build_or_rebuild(tmp_path, monkeypatch):
    graph = {'url':'http://127.0.0.1:8765', 'database':'admin/fixture'}
    svc = ServiceContext(tmp_path/'service', runtime=SimpleNamespace(runtime_root=tmp_path))
    owner, pubs, content = setup_atlas(tmp_path/'one', svc, graph)
    other, _, _ = setup_atlas(tmp_path/'two', svc, graph)
    owner_database = owner.project_binding.graph_database
    pubs.publish(content, lambda c:{**projection(c), 'database':owner_database})
    other = replace(other, configuration={'graph': {**graph, 'database': owner_database}})
    monkeypatch.setattr(native_pipeline, 'validate_runtime', lambda rt:rt)
    real_run = native_pipeline.subprocess.run
    def no_native(argv, **kwargs):
        assert 'knowledge_atlas.native_pipeline' not in argv, 'second atlas reached native graph writer'
        return real_run(argv, **kwargs)
    monkeypatch.setattr(native_pipeline.subprocess,'run', no_native)
    for action in ('publication.build','publication.rebuild'):
        with pytest.raises(AtlasError) as error:
            native_pipeline.run_native(replace(other, request=replace(other.request, action=action)))
        assert error.value.code == 'graph_atlas_conflict' and error.value.status == 409
    native_pipeline.claim_projection_database(owner)


def test_concurrent_first_writers_claim_distinct_databases_and_relocation_preserves_owner(tmp_path):
    graph = {'url':'http://127.0.0.1:80/','database':'admin/fixture'}
    service = ServiceContext(tmp_path/'service', configuration={'graph': graph})
    first, second = [setup_atlas(tmp_path/name, service, graph)[0] for name in ('one','two')]
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(native_pipeline.claim_projection_database, (first, second)))
    with database(service) as db:
        owners = [tuple(row) for row in db.execute('SELECT database_name,atlas_id FROM projection_owners')]
    assert set(owners) == {(ctx.project_binding.graph_database, ctx.request.atlas_id) for ctx in (first, second)}

    moved = first.atlas_path.rename(tmp_path/'relocated')
    register_atlas(service, first.request.atlas_id, moved)
    request = replace(first.request, action='publication.rebuild')
    relocated = ActionContext.for_read(service, request)
    assert relocated.atlas_path == moved
    assert relocated.project_binding.graph_database == first.project_binding.graph_database
    native_pipeline.claim_projection_database(relocated)


def test_unusable_cache_fails_before_graph_access(tmp_path, monkeypatch):
    from knowledge_atlas.core import native_projection
    graph = {'url':'http://127.0.0.1:8765','database':'admin/fixture'}
    ctx, pubs, content = setup_atlas(tmp_path/'one', ServiceContext(tmp_path/'svc'), graph)
    wrong_cache = tmp_path/'regular-file'
    wrong_cache.write_text('unusable')
    graph_service = Mock(database='admin/fixture')
    graph_service.call.side_effect = AssertionError('cache failure reached graph')
    monkeypatch.setattr(native_projection, 'chunks_for', lambda *a: [])
    with pytest.raises(OSError):
        NativeProjection(pubs.store,graph_service,None,None,wrong_cache).build(content)
    graph_service.call.assert_not_called()



def test_projection_rejects_an_unbound_database_before_claiming_it(tmp_path):
    graph = {'url': 'http://127.0.0.1:8765', 'database': 'admin/inherited'}
    service = ServiceContext(tmp_path / 'service', configuration={'graph': graph})
    ctx, _, _ = setup_atlas(tmp_path / 'atlas', service, graph)
    attempt = replace(ctx, configuration={'graph': graph})

    with pytest.raises(AtlasError, match='graph_atlas_conflict'):
        native_pipeline.claim_projection_database(attempt)
    with database(service) as db:
        assert db.execute('SELECT count(*) FROM projection_owners').fetchone()[0] == 0


@pytest.mark.parametrize('damage', ['version', 'table', 'row', 'digest'])
def test_damaged_vector_storage_is_untouched_before_graph_access(tmp_path, monkeypatch, damage):
    from knowledge_atlas.core import native_projection
    from test_project_registry import vector_fixture
    cache = tmp_path / 'vectors'
    source, _ = vector_fixture(cache)
    with source.connect() as db:
        db.execute({'version': 'PRAGMA user_version=0', 'table': 'DROP TABLE vector_integrity',
                    'row': 'DELETE FROM vector_integrity', 'digest': "UPDATE vector_integrity SET sha256='bad'"}[damage])
    path = cache / 'vectors.sqlite3'
    before = path.read_bytes()
    graph = {'url':'http://127.0.0.1:8765','database':'admin/fixture'}
    ctx, pubs, content = setup_atlas(tmp_path / 'one', ServiceContext(tmp_path / 'svc'), graph)
    graph_service = Mock(database='admin/fixture')
    graph_service.call.side_effect = AssertionError('cache failure reached graph')
    monkeypatch.setattr(native_projection, 'chunks_for', lambda *a: [])
    with pytest.raises(ValueError, match='vector'):
        NativeProjection(pubs.store, graph_service, None, None, cache).build(content)
    assert path.read_bytes() == before
    graph_service.call.assert_not_called()


def test_projection_requires_current_binding_before_claim_on_an_unregistered_store(tmp_path):
    graph = {'url':'http://127.0.0.1:8765', 'database':'admin/inherited'}
    service = ServiceContext(tmp_path / 'service', configuration={'graph': graph})
    ctx, _, _ = setup_atlas(tmp_path / 'atlas', service, graph)
    fresh = ServiceContext(tmp_path / 'unregistered')
    with pytest.raises(AtlasError, match='graph_atlas_conflict'):
        native_pipeline.claim_projection_database(replace(ctx, service=fresh, configuration={'graph': graph},
                                                         project_binding=None))
    assert not fresh.service_root.exists()
