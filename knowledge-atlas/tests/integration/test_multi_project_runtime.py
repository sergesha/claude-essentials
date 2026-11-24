"""Two registered projects on the Docker supervisor's complete prepared native stack."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from knowledge_atlas.client import AtlasClient
from knowledge_atlas.contracts import ActionRequest, AtlasError
from knowledge_atlas.core.git_store import GitStore
from knowledge_atlas.core.graph_stage import GraphStage
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.launcher import service_context
from knowledge_atlas.operations import submit

PACKAGE = Path(__file__).resolve().parents[2]


@contextmanager
def running_stack(data, runtime):
    env = {**os.environ, 'ATLAS_DATA_ROOT': str(data), 'ATLAS_RUNTIME_ROOT': str(runtime)}
    with (data / 'supervisor.log').open('a') as log:
        process = subprocess.Popen([sys.executable, str(PACKAGE / 'docker/entrypoint.py'), 'start'],
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                assert process.poll() is None, (data / 'supervisor.log').read_text()
                try:
                    client = AtlasClient(data / 'service')
                    client.verify_ready()
                    break
                except AtlasError:
                    time.sleep(.2)
            else:
                raise AssertionError('supervised service did not start')
            yield client
        finally:
            process.terminate()
            process.wait(timeout=60)


def completed(client, operation):
    deadline = time.monotonic() + 240
    while operation.state in ('queued', 'running') and time.monotonic() < deadline:
        time.sleep(.2)
        operation = client.operation(operation.operation_id)
    assert operation.state == 'succeeded', operation.error
    return operation.result


def decision(path, identity, label, previous=None):
    store = GitStore(path)
    day = 15 if previous else 14
    now = f'2026-11-{day}T01:00:00Z'
    wiki = {'sources': [], 'pages': [{'id': 'page', 'path': '', 'filename': 'notes',
                                     'content': label}], 'references': []}
    extraction = {'entities': [], 'statements': []}
    if previous:
        from knowledge_atlas.core.feedback import revise_compilation
        base = Publications(store).read(previous)['content_commit']
        candidate = revise_compilation(store, base, [], wiki, extraction, {},
            actor='Fixture', method='Synthetic', reason='Changed page', recorded_at=now,
            base_publication=previous)
    else:
        candidate = store.snapshot(wiki, extraction, {}, recorded_at=now)['commit']
    return ActionRequest('candidate.decide', atlas_id=identity, request_id=str(uuid.uuid4()),
                         expected_publication=previous, payload={
        'candidate': candidate, 'actor': 'Fixture', 'kind': 'synthetic', 'action': 'accept',
        'reason': 'Independent native projection', 'decided_at': f'2026-11-{day}T02:00:00Z'})


def state(client, path, identity, graph_url):
    ready = client.call(ActionRequest('atlas.status', atlas_id=identity)).publication
    receipt = Publications(GitStore(path)).read(ready)
    graph = GraphStage(graph_url, receipt['database'])
    documents = graph.documents(receipt['graph_commit'])
    assert receipt['vector_artifacts'] and any(row['@type'] == 'Chunk' for row in documents)
    # Check the current native database as well as the historical publication commit.
    head = graph.call('GET', '/api/document/' + graph.database + '?as_list=true')
    vectors = graph.call('GET', '/api/plugin/search-embeddings/' + graph.database + '/local/commit/'
                         + receipt['graph_commit'] + '?compress_ids=false')['body']
    assert vectors['served_commit'] == receipt['graph_commit'] and vectors['doc_embeddings']
    return {'atlas_id': identity, 'publication': ready, 'database': graph.database,
            'vectors_sha256': hashlib.sha256(json.dumps(vectors['doc_embeddings'], sort_keys=True).encode()).hexdigest(),
            'graph_commit': receipt['graph_commit'], 'head': head['headers']['terminusdb-data-version'],
            'documents_sha256': hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()}


def test_production_two_projects_independent_and_restart(runtime_configuration, tmp_path, monkeypatch):
    # The supervisor uses its production loopback ports. Run in an isolated network namespace.
    runtime = Path(runtime_configuration['runtime_root'])
    with tempfile.TemporaryDirectory() as private:
        data = Path(private)
        monkeypatch.setenv('KNOWLEDGE_ATLAS_GRAPH_KEY_FILE', str(data / 'graph/admin.key'))
        profiles = json.loads((PACKAGE / 'runtime/model-profiles.json').read_text())
        (data / 'profiles.json').write_text(json.dumps({k: {**v, 'provider': 'codex'} for k, v in profiles.items()}))
        paths = [data / 'projects' / name for name in ('A', 'B')]
        ids = []
        with running_stack(data, runtime) as client:
            config = json.loads((data / 'service-config.json').read_text())
            graph = config['configuration']['graph']
            assert set(graph) == {'url'} and client.atlas_list().data['projects'] == []
            for path in paths:
                created = completed(client, client.call(ActionRequest('atlas.create', request_id=str(uuid.uuid4()),
                                                                      payload={'path': str(path)})))
                identity = created.data['atlas_id']
                ids.append(identity)
                result = completed(client, client.call(decision(path, identity, path.name + ' native evidence')))
                assert result.publication, result.data.get('publication_error', result.data)
            before = [state(client, path, identity, graph['url']) for path, identity in zip(paths, ids)]
            assert before[0]['database'] != before[1]['database']

        # Enqueue while the dispatcher is stopped, then let normal startup pick up the durable request.
        queued = submit(service_context(config), decision(paths[0], ids[0], 'A revised evidence', before[0]['publication']))
        assert queued.state == 'queued'
        with running_stack(data, runtime) as client:
            result = completed(client, client.operation(queued.operation_id))
            assert result.publication, result.data.get('publication_error', result.data)
            assert {p['atlas_id'] for p in client.atlas_list().data['projects']} == set(ids)
            after = [state(client, path, identity, graph['url']) for path, identity in zip(paths, ids)]
            assert after[1] == before[1]
            assert after[0]['database'] == before[0]['database']
            assert after[0]['publication'] == result.publication != before[0]['publication']
            assert after[0]['head'] != before[0]['head']
        (tmp_path / 'multi-project.json').write_text(json.dumps({
            'before': before, 'after': after, 'queued_operation': queued.operation_id}, indent=2))
