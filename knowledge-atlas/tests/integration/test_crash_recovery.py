"""Opt-in actual SIGKILL recovery; mount location is selected by evidence-dir."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def test_native_crash_preserves_history_vectors_and_atomic_writes(native_graph,tmp_path):
    package=Path(__file__).resolve().parents[2]
    config={k:str(native_graph[k]) for k in ('runtime','atlas')}
    config['graph_url']=native_graph['graph'].url
    script="import json,runpy,sys; from pathlib import Path; runpy.run_path(sys.argv[1])['_check_native_projection'](json.loads(sys.argv[2]),Path(sys.argv[3]))"
    run=subprocess.run([str(native_graph['runtime']/'python/bin/python'),'-c',script,
        str(Path(__file__).with_name('test_runtime.py')),json.dumps(config),str(tmp_path)],
        env={**os.environ,'PYTHONPATH':str(package/'src')},capture_output=True,text=True)
    (tmp_path/'projection.stdout').write_text(run.stdout)
    (tmp_path/'projection.stderr').write_text(run.stderr)
    assert run.returncode==0,run.stderr
    publications=json.loads((tmp_path/'qualification.json').read_text())['publications']
    graph=native_graph['graph']
    def fingerprints():
        values=[]
        for publication,content,commit in publications:
            docs=sorted(graph.documents(commit),key=lambda x:x['@id'])
            vectors=graph.call('GET','/api/plugin/search-embeddings/'+graph.database+
                '/local/commit/'+commit+'?compress_ids=false')['body']
            assert vectors['served_commit']==commit
            values.append({'publication':publication,'content':content,'graph_commit':commit,
                'documents_sha256':digest(docs),'vectors_sha256':digest(vectors['doc_embeddings'])})
        return values
    before=fingerprints()
    with urllib.request.urlopen(native_graph['bridge']+'/stats') as response:
        inference_before=json.load(response)['inference_calls']
    native_graph['crash_restart_graph']()
    assert fingerprints()==before
    with urllib.request.urlopen(native_graph['bridge']+'/stats') as response:
        assert json.load(response)['inference_calls']==inference_before
    # A separate database isolates a deliberately ambiguous in-flight transaction.
    path='/api/document/admin/crashwrite'
    graph.call('POST','/api/db/admin/crashwrite',{'label':'Synthetic crash test','schema':True})
    graph.call('POST',path+'?graph_type=schema&author=test&message=schema',
        [{'@type':'Class','@id':'Probe','value':'xsd:string'}])
    baseline=[{'@type':'Probe','@id':'Probe/baseline','value':'acknowledged'}]
    graph.call('POST',path+'?author=test&message=baseline',baseline)
    rows=[{'@type':'Probe','@id':'Probe/item'+str(n),'value':'x'*4096} for n in range(2000)]
    outcome={};started=threading.Event()
    def writer():
        started.set()
        try:
            graph.call('POST',path+'?full_replace=true&author=test&message=inflight',rows)
            outcome['client']='acknowledged'
        except Exception as error:
            outcome['client']='interrupted';outcome['error_type']=type(error).__name__
    thread=threading.Thread(target=writer)
    thread.start();assert started.wait(5)
    time.sleep(.05)
    assert thread.is_alive(),'write completed before interruption; no in-flight coverage'
    native_graph['crash_restart_graph']()
    thread.join(timeout=65);assert not thread.is_alive()
    actual=graph.call('GET',path+'?as_list=true')['body']
    normalized={x['@id'].split('/data/')[-1]:x['value'] for x in actual}
    old={'Probe/baseline':'acknowledged'}
    new={x['@id']:x['value'] for x in rows}
    assert normalized in (old,new),'partial transaction visible after recovery'
    assert fingerprints()==before
    graph.call('POST',path+'?author=test&message=post-recovery',
        [{'@type':'Probe','@id':'Probe/after','value':'writable after recovery'}])
    assert any(x['value']=='writable after recovery' for x in graph.call('GET',path+'?as_list=true')['body'])
    (tmp_path/'crash-recovery.json').write_text(json.dumps({'status':'passed','signal':'SIGKILL',
        'restarts':2,'publications':before,'inflight':outcome,
        'transaction_visibility':'old' if normalized==old else 'new',
        'inference_calls_during_first_recovery':0,'post_recovery_write':True,
        'limits':['process crash only','not host power loss or Docker VM crash','one synthetic in-flight transaction']},indent=2))
