"""Actual native replay, retained historical reads, and separate fresh inference."""
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid


def test_native_archive_history_and_embedding_roles(runtime_configuration, request, tmp_path):
    import pytest
    if not runtime_configuration.get('archive'):
        pytest.skip('native archive qualification requires an explicit retained archive')
    native_graph = request.getfixturevalue('native_graph')
    config = {'atlas':str(native_graph['atlas']), 'graph':{'url':native_graph['graph'].url,
        'database':native_graph['graph'].database}, 'bridge':native_graph['bridge']}
    script="import runpy,json,sys; runpy.run_path(sys.argv[1])['_check_archive'](json.loads(sys.argv[2]))"
    result = subprocess.run([str(native_graph['runtime']/'python/bin/python'),'-c',script,
        str(Path(__file__).resolve()),json.dumps(config)],
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[2]/'src')},capture_output=True,text=True)
    (tmp_path/'archive.stdout').write_text(result.stdout)
    (tmp_path/'archive.stderr').write_text(result.stderr)
    assert result.returncode==0, result.stderr


def _check_archive(config):
    from knowledge_atlas.core.archive import rebuild_projections
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
        store.base/'assets/chunking.py',store.base/'vectors'))
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
