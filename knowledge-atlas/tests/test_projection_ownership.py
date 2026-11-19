"""A fixed native graph database has one publishing atlas per service."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from knowledge_atlas import native_pipeline
from knowledge_atlas.contracts import ActionContext, ActionRequest, AtlasError, ServiceContext
from knowledge_atlas.core.git_store import GitStore
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.native_projection import NativeProjection
from knowledge_atlas.operations import register_atlas
from knowledge_atlas.runtime import _assets
from test_decisions import projection
from test_packages import snapshot


def setup_atlas(path, service, graph):
    store = GitStore(path)
    candidate = snapshot(store)
    manifest = store.read(candidate)['manifest']
    register_atlas(service, manifest['package_id'], path)
    pub = Publications(store)
    profile = json.loads((_assets()/'embedding-profile.json').read_text())
    accepted = pub.review(candidate, {'decision':'accept','kind':'synthetic','actor':'Fixture',
        'reason':'Ownership test','accepted_at':'2026-11-13T01:00:00Z','base_publication':None}, profile)
    ctx = ActionContext(service, ActionRequest('publication.build', payload={'content':accepted}),
                        path, path/'evidence', {'graph':graph})
    return ctx, pub, accepted


def test_existing_publication_prevents_another_atlas_from_entering_native_build_or_rebuild(tmp_path, monkeypatch):
    graph = {'url':'http://127.0.0.1:8765', 'database':'admin/fixture'}
    svc = ServiceContext(tmp_path/'service', runtime=SimpleNamespace(runtime_root=tmp_path))
    owner, pubs, content = setup_atlas(tmp_path/'one', svc, graph)
    other, _, _ = setup_atlas(tmp_path/'two', svc, graph)
    pubs.publish(content, lambda c:{**projection(c), 'database':graph['database']})
    monkeypatch.setattr(native_pipeline, 'validate_runtime', lambda rt:rt)
    real_run = native_pipeline.subprocess.run
    def no_native(argv, **kwargs):
        assert 'knowledge_atlas.native_pipeline' not in argv, 'second atlas reached native graph writer'
        return real_run(argv, **kwargs)
    monkeypatch.setattr(native_pipeline.subprocess,'run', no_native)
    for action in ('publication.build','publication.rebuild'):
        with pytest.raises(AtlasError) as error:
            native_pipeline.run_native(replace(other, request=ActionRequest(action)))
        assert error.value.code == 'graph_atlas_conflict' and error.value.status == 409
    native_pipeline.claim_projection_database(owner)


def test_concurrent_first_writers_claim_one_owner_and_relocated_owner_can_rebuild(tmp_path):
    graph = {'url':'http://127.0.0.1:80/','database':'admin/fixture'}
    svc = ServiceContext(tmp_path/'service')
    contexts = [setup_atlas(tmp_path/name, svc, graph)[0] for name in ('one','two')]
    contexts[1] = replace(contexts[1], configuration={'graph':{**graph, 'url':'http://127.0.0.1'}})
    def try_claim(ctx):
        try:
            native_pipeline.claim_projection_database(ctx)
            return ctx
        except AtlasError as error:
            assert error.code == 'graph_atlas_conflict'
    with ThreadPoolExecutor(max_workers=2) as workers:
        winners = [c for c in workers.map(try_claim,contexts) if c]
    assert len(winners) == 1
    winner = winners[0]
    moved = winner.atlas_path.rename(tmp_path/'relocated')
    native_pipeline.claim_projection_database(replace(winner, atlas_path=moved,
        request=ActionRequest('publication.rebuild')))


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
