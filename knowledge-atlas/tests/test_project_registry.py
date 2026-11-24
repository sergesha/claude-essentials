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
    ready = pubs.publish(content, lambda commit: {**projection(commit), 'database': 'admin/retained'})
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
