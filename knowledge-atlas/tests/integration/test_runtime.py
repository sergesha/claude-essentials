"""Real pinned CPU embedding qualification; no inferred platform coverage."""
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request


def test_native_gemma_profile_and_roles(runtime_configuration, tmp_path):
    from knowledge_atlas.contracts import RuntimeConfig
    from knowledge_atlas.runtime import validate_runtime
    from knowledge_atlas.core.embedding_provider import EmbeddingProvider
    runtime = Path(runtime_configuration['runtime_root'])
    identity = validate_runtime(RuntimeConfig(runtime))
    assert identity.runtime_root == runtime.resolve()
    package = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    endpoint = f'http://127.0.0.1:{port}'
    with (tmp_path/'gemma.log').open('w') as log:
        process = subprocess.Popen([str(runtime/'python/bin/python'), str(package/'runtime/gemma-service.py')],
            env={**os.environ, 'EMBED_MODEL_PATH':str(runtime/'model'), 'EMBED_PORT':str(port)}, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise AssertionError((tmp_path/'gemma.log').read_text())
                try:
                    with urllib.request.urlopen(endpoint+'/health', timeout=1) as response:
                        assert json.load(response)['status'] == 'ok'
                    break
                except OSError:
                    assert time.monotonic() < deadline, 'Gemma startup timed out'
                    time.sleep(.2)
            provider = EmbeddingProvider(package/'runtime/embedding-profile.json', tmp_path/'vectors', endpoint)
            vectors = []
            for role in ('document','query'):
                body = {'model':provider.profile['model_namespace'], 'dimensions':256,
                        'role':role, 'input':['A pump maintains pressure.']}
                vector = provider.embed(body)['data'][0]['embedding']
                assert len(vector) == 256
                assert all(math.isfinite(v) for v in vector)
                assert sum(v*v for v in vector) > 0
                vectors.append(vector)
                assert provider.embed(body)['data'][0]['embedding'] == vector
            assert vectors[0] != vectors[1]
            assert provider.stats['inference_calls'] == 2
            assert provider.stats['cache_hits'] == 2
            with provider.connect() as db:
                artifacts = [json.loads(row[0]) for row in db.execute('SELECT artifact FROM vectors')]
            assert {a['role'] for a in artifacts} == {'document','query'}
            assert {a['input'] for a in artifacts} == {'title: none | text: A pump maintains pressure.',
                'task: search result | query: A pump maintains pressure.'}
            for artifact in artifacts:
                assert len(artifact['raw768']) == 768
                assert abs(sum(v*v for v in artifact['raw768']) - 1) < 1e-5
        finally:
            process.terminate()
            process.wait(timeout=20)


def test_native_historical_projection_and_eligible_top_k(native_graph, tmp_path):
    config = {k:str(native_graph[k]) for k in ('runtime','atlas','cache')}
    config['graph_url'] = native_graph['graph'].url
    script = "import json,runpy,sys; from pathlib import Path; runpy.run_path(sys.argv[1])['_check_native_projection'](json.loads(sys.argv[2]),Path(sys.argv[3]))"
    result = subprocess.run([str(native_graph['runtime']/'python/bin/python'), '-c', script,
        str(Path(__file__).resolve()), json.dumps(config),str(tmp_path)],
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[2]/'src')},capture_output=True,text=True)
    (tmp_path/'projection.stdout').write_text(result.stdout)
    (tmp_path/'projection.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stderr


def _check_native_projection(native_graph, tmp_path):
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.core.publication import Publications
    from runpy import run_path
    review_acceptance = run_path(str(Path(__file__).resolve().parents[1] / "acceptance_fixture.py"))["review_acceptance"]
    from knowledge_atlas.core.native_projection import NativeProjection
    from knowledge_atlas.core.graph_stage import records_for
    from knowledge_atlas.core.reader import AtlasReader
    package = Path(__file__).resolve().parents[2]
    fixture = package/'tests/fixtures/evidence'
    wiki = json.loads((fixture/'wiki-current.json').read_text())
    extraction = json.loads((fixture/'extraction-current.json').read_text())
    store = GitStore(native_graph['atlas'])
    pubs = Publications(store)
    profile = json.loads((package/'runtime/embedding-profile.json').read_text())
    from knowledge_atlas.core.graph_stage import GraphStage
    graph = GraphStage(native_graph['graph_url'], 'admin/qualification'); runtime = Path(native_graph['runtime'])
    builder = NativeProjection(store, graph, runtime/'model/tokenizer.json', runtime/'chunking.py', native_graph['cache'])
    published = []
    for day in (13,14):
        if day == 14: wiki['pages'][0]['content'] += '\nQualification update: inspection remains required.\n'
        recorded_at = f'2026-11-{day}T00:00:00Z'
        if published:
            from knowledge_atlas.core.feedback import revise_compilation
            candidate = revise_compilation(store, published[-1][1], [], wiki, extraction, {},
                actor='Fixture', method='Native qualification', reason='Changed page',
                recorded_at=recorded_at, base_publication=published[-1][0])
        else:
            candidate = store.snapshot(wiki, extraction, {}, recorded_at=recorded_at)['commit']
        content = review_acceptance(pubs, candidate, {'decision':'accept','kind':'synthetic','actor':'Runtime qualification',
            'reason':'Retained PoC9 fixture, technical qualification only',
            'accepted_at':f'2026-11-{day}T01:00:00Z', 'base_publication':pubs.ready()}, profile)
        publication = pubs.publish(content, builder.build)
        receipt = pubs.read(publication)
        assert receipt['vector_max_error'] <= 1e-6
        published.append((publication,content,receipt['graph_commit']))
    for publication, content, graph_commit in published:
        records = records_for(store.read(content), content)
        allowed = {row['@id'] for row in records}
        reader = AtlasReader(store, graph.url, lambda:allowed, publication=publication)
        eligible = {next(row['@id'] for row in records if row['kind']=='source')}
        for mode in ('vector','hybrid'):
            result = reader.search('inspection', mode=mode, count=1, eligible=eligible)
            assert result['status']=='matches', result
            assert result['graph_commit']==graph_commit
            assert {hit['record']['@id'] for hit in result['hits']}==eligible
        assert len([d for d in graph.documents(graph_commit) if d['@type']=='Record'])==len(records)
    assert published[0][2]!=published[1][2]
    (tmp_path/'qualification.json').write_text(json.dumps({'publications':published,'runtime':str(runtime),
        'graph_trace':graph.trace},indent=2))
