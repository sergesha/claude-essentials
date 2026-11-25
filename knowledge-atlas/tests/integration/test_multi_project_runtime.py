"""One production service with three independently routed native projections."""
from contextlib import contextmanager, nullcontext
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from knowledge_atlas import __version__
from knowledge_atlas.client import AtlasClient
from knowledge_atlas.contracts import ActionRequest, AtlasError
from knowledge_atlas.core.feedback import revise_compilation
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.graph_stage import GraphStage
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.launcher import service_context
from knowledge_atlas.locks import FileLock
from knowledge_atlas.operations import submit
from knowledge_atlas.providers.config import default_profiles

PACKAGE = Path(__file__).resolve().parents[2]


@contextmanager
def running_stack(data, runtime, log_path, *, packaged):
    if packaged:
        command = [sys.executable, str(PACKAGE / 'docker/entrypoint.py'), 'start']
        env = {**os.environ, 'ATLAS_DATA_ROOT': str(data), 'ATLAS_RUNTIME_ROOT': str(runtime)}
    else:
        command = [sys.executable, '-m', 'knowledge_atlas.cli', 'serve', '--config',
                   str(data / 'service-config.json')]
        env = os.environ.copy()
    with log_path.open('a') as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()
                try:
                    client = AtlasClient(data / 'service')
                    assert client.verify_ready() == __version__
                    break
                except AtlasError:
                    time.sleep(.2)
            else:
                raise AssertionError('Atlas service did not start; see ' + str(log_path))
            yield client
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def mutation(action, identity=None, /, *, expected=None, **payload):
    return ActionRequest(action, atlas_id=identity, request_id=str(uuid.uuid4()),
                         expected_publication=expected, payload=payload)


def completed(client, operation):
    deadline = time.monotonic() + 240
    while operation.state in ('queued', 'running') and time.monotonic() < deadline:
        time.sleep(.2)
        operation = client.operation(operation.operation_id)
    assert operation.state == 'succeeded', (operation.state, operation.error, operation.evidence)
    return operation.result


def candidate(path, label, previous=None):
    store = GitStore(path)
    now = '2026-11-14T01:00:00Z'
    wiki = {'sources': [], 'pages': [{'id': 'page', 'path': '', 'filename': 'notes',
                                     'content': label}], 'references': []}
    extraction = {'entities': [], 'statements': []}
    if previous:
        base = Publications(store).read(previous)['content_commit']
        return revise_compilation(store, base, [], wiki, extraction, {},
                                  actor='Fixture', method='Synthetic', reason='Changed page',
                                  recorded_at=now, base_publication=previous)
    return store.snapshot(wiki, extraction, {}, recorded_at=now)['commit']


def decision(identity, proposed, previous=None):
    return mutation('candidate.decide', identity, expected=previous, candidate=proposed, action='accept',
                    actor='Fixture', kind='synthetic', reason='Independent native projection',
                    decided_at='2026-11-14T02:00:00Z')


def published(result):
    assert result.data['status'] == 'published' and result.publication, result.data.get('publication_error', result.data)
    return result.publication


def state(client, path, identity, graph_url):
    status = client.call(ActionRequest('atlas.status', atlas_id=identity))
    assert status.data['atlas_id'] == identity and status.data['matching_decision_receipt']
    ready = status.publication
    receipt = Publications(GitStore(path)).read(ready)
    assert receipt['content_commit'] in status.data['accepted_content']
    graph = GraphStage(graph_url, receipt['database'])
    documents = graph.documents(receipt['graph_commit'])
    assert receipt['vector_artifacts'] and any(row['@type'] == 'Chunk' for row in documents)
    head = graph.call('GET', '/api/document/' + graph.database + '?as_list=true')
    vectors = graph.call('GET', '/api/plugin/search-embeddings/' + graph.database + '/local/commit/'
                         + receipt['graph_commit'] + '?compress_ids=false')['body']
    assert vectors['served_commit'] == receipt['graph_commit'] and vectors['doc_embeddings']
    return {'atlas_id': identity, 'publication': ready, 'database': graph.database,
            'content_commit': receipt['content_commit'], 'graph_commit': receipt['graph_commit'],
            'head': head['headers']['terminusdb-data-version'],
            'vectors_sha256': hashlib.sha256(json.dumps(vectors['doc_embeddings'], sort_keys=True).encode()).hexdigest(),
            'documents_sha256': hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()}


async def mcp_projects(config_path, expected):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'knowledge_atlas.cli', 'mcp', '--config', str(config_path)], env=dict(os.environ))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.version == __version__
            listed = await session.call_tool('atlas_atlas_list', {'payload': {}})
            assert not listed.isError, listed.structuredContent
            rows = listed.structuredContent['data']['projects']
            assert {row['atlas_id']: row['publication'] for row in rows} == expected
            for atlas_id, publication in expected.items():
                opened = await session.call_tool('atlas_atlas_open', {'atlas_id': atlas_id, 'payload': {}})
                assert not opened.isError, opened.structuredContent
                assert opened.structuredContent['data']['atlas_id'] == atlas_id
                assert opened.structuredContent['publication'] == publication
            rejected = await session.call_tool('atlas_atlas_list', {'payload': {'project': 'obsolete'}})
            assert rejected.isError and rejected.structuredContent['error']['code'] == 'invalid_request'
            listed_again = await session.call_tool('atlas_atlas_list', {'payload': {}})
            assert listed_again.structuredContent == listed.structuredContent
            return rows


def search(client, identity, query, publication=None):
    result = client.call(ActionRequest('search', atlas_id=identity, publication=publication,
                                      payload={'query': query, 'mode': 'fts'}))
    assert result.publication == result.data['publication_commit']
    if publication:
        assert result.publication == publication
    return result.data


def test_production_two_projects_independent_and_restart(runtime_configuration, native_graph_session,
                                                         tmp_path, monkeypatch):
    runtime = Path(runtime_configuration['runtime_root'])
    packaged = (runtime / 'runtime.json').resolve() == Path('/opt/atlas-seed/runtime.json')
    with tempfile.TemporaryDirectory(prefix='atlas-multiproject-') as private:
        data = Path(private)
        graph_fixture = nullcontext(None) if packaged else native_graph_session(runtime_configuration, data)
        with graph_fixture as native:
            config_path = data / 'service-config.json'
            profiles = default_profiles()
            if packaged:
                monkeypatch.setenv('KNOWLEDGE_ATLAS_GRAPH_KEY_FILE', str(data / 'graph/admin.key'))
                (data / 'profiles.json').write_text(json.dumps(profiles))
            else:
                config_path.write_text(json.dumps({'service_root': str(data / 'service'),
                    'runtime': runtime_configuration,
                    'configuration': {'profiles': profiles, 'graph': {'url': native['graph'].url}}}))

            def stack():
                return running_stack(data, runtime, tmp_path / 'supervisor.log', packaged=packaged)

            def configure(child_publication, expected):
                return mutation('packages.configure', composite, expected=expected,
                                includes=[{'package_id': a, 'mount': 'packages/child',
                                           'publication': child_publication}])

            paths = [data / 'projects' / name for name in ('A', 'B', 'Composite')]
            with stack() as client:
                config = json.loads(config_path.read_text())
                graph = config['configuration']['graph']
                assert set(graph) == {'url'} and client.atlas_list().data['projects'] == []
                ids = [completed(client, client.call(mutation('atlas.create', path=str(path)))).data['atlas_id']
                       for path in paths]
                a, b, composite = ids
                assert len(set(ids)) == 3
                initial = []
                labels = ('Saffron calibrates the sensor.', 'Cobalt marks the inventory.',
                          'Orchard tracks the composite notebook.')
                for path, identity, label in zip(paths, ids, labels):
                    initial.append(published(completed(client, client.call(decision(identity, candidate(path, label))))))
                a0, b0, own = initial
                before = [state(client, path, identity, graph['url']) for path, identity in zip(paths, ids)]
                assert len({item['database'] for item in before}) == 3
                assert search(client, b, 'saffron')['hits'] == []
                own_store = GitStore(paths[2])
                own_wiki = own_store.read(before[2]['content_commit'])['wiki']

                propose = completed(client, client.call(configure(a0, own)))
                assert propose.data['status'] == 'pending_review' and propose.publication == own
                child_update = decision(a, candidate(paths[0], 'Juniper replaces the calibration sample.', a0), a0)
                with FileLock(data / 'service' / 'writer.lock'):
                    compose = client.call(decision(composite, propose.data['candidate'], own))
                    advance = client.call(child_update)
                    assert compose.state == advance.state == 'queued'
                    assert client.call(ActionRequest('atlas.status', atlas_id=composite)).publication == own
                    assert search(client, a, 'saffron')['hits']
                    assert search(client, b, 'cobalt')['hits']
                seen = []
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    # Read the later operation first so a serial transition between
                    # polls cannot be mistaken for a concurrent writer.
                    second = client.operation(advance.operation_id)
                    first = client.operation(compose.operation_id)
                    seen.append((first.state, second.state))
                    if second.state != 'queued':
                        assert first.state == 'succeeded', seen
                        break
                    if first.state not in ('queued', 'running'):
                        assert first.state == 'succeeded', first.error
                    time.sleep(.1)
                c0 = published(completed(client, compose))
                a1 = published(completed(client, advance))
                assert seen and seen[-1][0] == 'succeeded' and seen[-1][1] in ('running', 'succeeded')
                assert a0 != a1 and own != c0
                assert search(client, a, 'juniper')['hits'] and search(client, a, 'saffron', a0)['hits']
                c_before = search(client, composite, 'saffron')
                assert c_before['publication_commit'] == c0 and c_before['hits']
                assert search(client, composite, 'juniper')['hits'] == []

                refs = git(own_store.root, 'show-ref')
                stale = client.call(configure(a1, own))
                deadline = time.monotonic() + 60
                while stale.state in ('queued', 'running') and time.monotonic() < deadline:
                    time.sleep(.1)
                    stale = client.operation(stale.operation_id)
                assert stale.state == 'failed' and stale.error['code'] == 'stale_publication'
                assert git(own_store.root, 'show-ref') == refs
                assert client.call(ActionRequest('atlas.status', atlas_id=composite)).publication == c0

                repin = completed(client, client.call(configure(a1, c0)))
                c1 = published(completed(client, client.call(decision(composite, repin.data['candidate'], c0))))
                composed = state(client, paths[2], composite, graph['url'])
                assert c1 != c0 and composed['publication'] == c1
                snapshot = own_store.read(composed['content_commit'])
                assert snapshot['wiki'] == own_wiki
                included, = snapshot['included_packages']
                assert included['package_id'] == a
                assert included['inclusions'] == [{'mount': 'packages/child', 'publication': a1}]
                assert search(client, composite, 'orchard')['hits']
                assert search(client, composite, 'juniper')['hits']
                assert search(client, composite, 'saffron', c0) == c_before
                assert search(client, composite, 'saffron')['hits'] == []
                listed = asyncio.run(mcp_projects(config_path, {a: a1, b: b0, composite: c1}))
                before_restart = [state(client, path, identity, graph['url']) for path, identity in zip(paths, ids)]
                assert before_restart[1] == before[1]

            # Submit while the dispatcher is stopped. The normal startup detects
            # the persisted request and uses its original UUID and publication.
            restart_update = decision(a, candidate(paths[0], 'Willow replaces the calibration sample.', a1), a1)
            queued = submit(service_context(config), restart_update)
            assert queued.state == 'queued'
            with stack() as client:
                a2 = published(completed(client, client.operation(queued.operation_id)))
                after = [state(client, path, identity, graph['url']) for path, identity in zip(paths, ids)]
                assert after[1:] == before_restart[1:]
                assert after[0]['database'] == before_restart[0]['database']
                assert after[0]['publication'] == a2 != a1
                for key in ('head', 'graph_commit', 'documents_sha256', 'vectors_sha256'):
                    assert after[0][key] != before_restart[0][key], key
                assert search(client, a, 'willow')['hits']
                assert search(client, composite, 'juniper')['hits']
                assert search(client, composite, 'willow')['hits'] == []
            (tmp_path / 'multi-project.json').write_text(json.dumps({
                'version': __version__, 'packaged_supervisor': packaged, 'projects': listed,
                'initial': before, 'before_restart': before_restart, 'after_restart': after,
                'historical_composite': c0, 'stale_operation': stale.operation_id,
                'queue_observation': seen, 'restart_operation': queued.operation_id}, indent=2))
