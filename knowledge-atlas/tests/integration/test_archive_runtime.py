"""Actual native replay, retained historical reads, and separate fresh inference."""
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid


def test_native_archive_history_and_embedding_roles(runtime_configuration, native_graph_session, tmp_path):
    from tempfile import TemporaryDirectory

    def native(graph, function, log, bindings):
        config = {'atlas': str(tmp_path/'atlas'), 'cache': str(graph['cache']),
                  'graph': {'url': graph['graph'].url, 'database': graph['graph'].database},
                  'bridge': graph['bridge'], 'runtime': str(graph['runtime']), 'bindings': bindings}
        script = "import runpy,json,sys; runpy.run_path(sys.argv[1])[sys.argv[2]](json.loads(sys.argv[3]))"
        result = subprocess.run([str(graph['runtime']/'python/bin/python'), '-c', script,
                                str(Path(__file__).resolve()), function, json.dumps(config)],
                               env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]/'src')},
                               capture_output=True, text=True)
        (tmp_path/(log + '.stdout')).write_text(result.stdout)
        (tmp_path/(log + '.stderr')).write_text(result.stderr)
        assert result.returncode == 0, result.stderr

    bindings = {}
    if not runtime_configuration.get('archive'):
        # VectorLink can reuse vectors across databases within one native service.
        # Stop the source service and discard its entire graph/cache before replay.
        with TemporaryDirectory(dir=tmp_path) as source:
            with native_graph_session(runtime_configuration, Path(source)) as graph:
                native(graph, '_create_reviewed_archive', 'create', {})
        runtime_configuration = {**runtime_configuration, 'archive': str(tmp_path/'composed.zip')}
        bindings = json.loads((tmp_path/'expected-bindings.json').read_text())
    with native_graph_session(runtime_configuration, tmp_path) as graph:
        native(graph, '_check_archive', 'archive', bindings)


def _create_reviewed_archive(config):
    import tempfile
    from knowledge_atlas.core.archive import export_atlas
    from knowledge_atlas.core.composition import configure_composition
    from knowledge_atlas.core.feedback import revise_compilation
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.core.graph_stage import GraphStage
    from knowledge_atlas.core.native_projection import NativeProjection
    from knowledge_atlas.core.publication import Publications
    from runpy import run_path
    review_acceptance = run_path(str(Path(__file__).resolve().parents[1] / "acceptance_fixture.py"))["review_acceptance"]

    directory = Path(config['atlas']).parent
    runtime = Path(config['runtime'])
    tokenizer, formatter = runtime/'model/tokenizer.json', runtime/'chunking.py'
    profile = json.loads((Path(__file__).resolve().parents[2]/'runtime/embedding-profile.json').read_text())

    def publish(store, oid, graph_name):
        pubs = Publications(store)
        content = review_acceptance(pubs, oid, {'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture',
            'reason': 'Review archive fixture', 'accepted_at': '2026-11-16T02:00:00Z',
            'base_publication': pubs.ready()}, profile)
        graph = GraphStage(config['graph']['url'], graph_name)
        projection = NativeProjection(store, graph, tokenizer, formatter, config['cache'])
        return pubs.publish(content, projection.build), content

    bindings = {}
    now = '2026-11-16T01:00:00Z'
    blank = {'sources': [], 'pages': [], 'references': []}
    extraction = {'entities': [], 'statements': []}
    with tempfile.TemporaryDirectory(dir=directory) as original:
        parent, child = [GitStore(Path(original)/name) for name in ('parent', 'child')]
        parent_content = parent.snapshot(blank, extraction, {}, recorded_at=now)['commit']
        previous_parent = previous_child = child_content = None
        for version in ('earlier', 'current'):
            wiki = {**blank, 'pages': [{'id': 'page', 'path': '', 'filename': 'notes',
                                       'content': version + ' reviewed archive evidence'}]}
            if previous_child:
                proposal = revise_compilation(child, child_content, [], wiki, extraction, {},
                    actor='Fixture', method='Synthetic', reason='Update reviewed page', recorded_at=now,
                    base_publication=previous_child)
            else:
                proposal = child.snapshot(wiki, extraction, {}, recorded_at=now)['commit']
            previous_child, child_content = publish(child, proposal, 'admin/source-child')
            inclusion = {'package_id': child.read(child_content)['manifest']['package_id'], 'mount': 'materials',
                         'publication': previous_child, 'commit': child_content}
            proposal = configure_composition(parent, parent_content,
                [{**inclusion, 'url': os.path.relpath(child.root, parent.root)}],
                recorded_at=now, base_publication=previous_parent)
            previous_parent, parent_content = publish(parent, proposal, 'admin/source-parent')
            bindings[previous_parent] = inclusion
        export_atlas(parent, directory/'composed.zip', tokenizer=tokenizer, formatter=formatter)
    assert not parent.base.exists() and not child.base.exists()
    (directory/'expected-bindings.json').write_text(json.dumps(bindings, indent=2))


def _check_archive(config):
    from knowledge_atlas.core.archive import rebuild_projections
    from knowledge_atlas.core.composition import composition_receipts
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.core.publication import Publications
    from knowledge_atlas.core.graph_stage import GraphStage, records_for
    from knowledge_atlas.core.native_projection import NativeProjection
    from knowledge_atlas.core.reader import AtlasReader
    store=GitStore(config['atlas'])
    pubs=Publications(store);ready=pubs.ready()
    assert pubs.read(ready)['previous_publication'], 'qualification requires historical publications'
    endpoint=config['bridge']
    def stats():
        with urllib.request.urlopen(endpoint+'/stats',timeout=2) as response: return json.load(response)
    initial=stats()
    assert initial['inference_calls']==0
    graph=GraphStage(config['graph']['url'],config['graph']['database'])
    rebuilt=rebuild_projections(store,NativeProjection(store,graph,store.base/'assets/tokenizer.json',
        store.base/'assets/chunking.py',config['cache']))
    assert rebuilt['publications']>=2
    assert stats()['inference_calls']==0
    assert stats()['cache_hits']>0
    allowed=set(); current=ready
    while current:
        receipt=pubs.read(current)
        for record in records_for(store.read(receipt['content_commit']),receipt['content_commit']):
            allowed.add(record['@id']);allowed.update(record.get('links', []))
        current=receipt['previous_publication']
    current=ready
    while current:
        receipt=pubs.read(current)
        if config['bindings']:
            assert composition_receipts(store, receipt['content_commit']) == [config['bindings'][current]]
            assert all(node['path'].is_relative_to(store.base) for node in store.closure(current))
        records=records_for(store.read(receipt['content_commit']),receipt['content_commit'])
        reader=AtlasReader(store,config['graph']['url'],lambda:allowed,publication=current)
        native_records=[r for r in reader.graph.documents(reader.receipt['graph_commit']) if r['@type']=='Record']
        assert len(native_records)==len(records)
        for record in records:
            assert reader.retrieve_evidence(record['@id'])
        current=receipt['previous_publication']
    profile=json.loads((store.base/'assets/embedding-profile.json').read_text())
    body={'role':'query','model':profile['model_namespace'],'dimensions':256,
          'input':['Fresh archive qualification '+str(uuid.uuid4())]}
    request=urllib.request.Request(endpoint+'/v1/embeddings',data=json.dumps(body).encode(),
                                   headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=120) as response: result=json.load(response)
    assert len(result['data'][0]['embedding'])==256
    assert stats()['inference_calls']==1
    assert pubs.ready()==ready
    print(json.dumps({'publications': rebuilt['publications'], 'bindings': config['bindings'],
                      'document_inference_calls': 0, 'final_bridge_stats': stats()}))
