"""Portable canonical decisions; controlled vectors are not native qualification."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import uuid
import zipfile
import pytest
from knowledge_atlas.core.archive import export_atlas, restore_atlas
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.embedding_provider import EmbeddingProvider, canonical
from knowledge_atlas.actions.decisions import decide_candidate, revise_candidate, decision_inventory
from test_packages import snapshot
from test_decisions import context_for


@pytest.fixture
def portable(tmp_path,monkeypatch):
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.runtime import _assets
    assets=tmp_path/'assets';assets.mkdir()
    tokenizer,formatter=assets/'tokenizer.json',assets/'chunking.py'
    tokenizer.write_text('controlled tokenizer fixture');formatter.write_text('# controlled formatter fixture\n')
    profile=json.loads((_assets()/'embedding-profile.json').read_text())
    profile['profile']['endpoint']['tokenizer_sha256']=hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    profile['profile']['formatter_sha256']=hashlib.sha256(formatter.read_bytes()).hexdigest()
    profile['profile_id']=hashlib.sha256(canonical(profile['profile']).encode()).hexdigest()
    (assets/'embedding-profile.json').write_text(json.dumps(profile))
    text='title: none | text: Retained controlled document'
    artifact={'profile_id':profile['profile_id'],'profile':profile['profile'],'role':'document','input':text,
        'input_sha256':hashlib.sha256(text.encode()).hexdigest(),'raw768':[1.0]+[0.0]*767}
    def projection(content):
        return {'content_commit':content,'graph_commit':'controlled-'+content,'search_checkpoint':'controlled-'+content,
                'database':'admin/fixture','ready':True,'profile_id':profile['profile_id'],
                'record_count':1,'chunk_count':1,'vector_artifacts':{'fixture':artifact}}
    monkeypatch.setattr(decisions,'_assets',lambda:assets)
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:projection(content))
    store=GitStore(tmp_path/'atlas');candidate=snapshot(store)
    content=Publications(store).review(candidate,{'decision':'accept','kind':'synthetic','actor':'Fixture',
        'reason':'Controlled archive fixture','accepted_at':'2026-11-13T01:00:00Z','base_publication':None},profile)
    publication=Publications(store).publish(content,projection)
    return store,tokenizer,formatter,publication,profile,projection


def archive( portable, destination):
    return export_atlas(portable[0],destination,tokenizer=portable[1],formatter=portable[2])


def with_decisions(portable,tmp_path):
    store,_,_,publication,_,_=portable
    content=Publications(store).read(publication)['content_commit']
    candidate=propose_records(store,content,[],actor='Fixture',method='Explicit review',reason='Decision archive',
        recorded_at='2026-11-13T02:00:00Z',base_publication=publication)
    rejected=decide_candidate(context_for(tmp_path,store,candidate,'reject',publication))
    revision=context_for(tmp_path,store,candidate,'reject',publication)
    revision=replace(revision,request=replace(revision.request,action='candidate.revise',payload={
        **revision.request.payload,'entries':[]}))
    revised=revise_candidate(revision)
    accepted=decide_candidate(context_for(tmp_path,store,revised.data['candidate'],'accept',publication))
    return rejected,revised,accepted


def test_decision_roundtrip_without_service_db(portable,tmp_path):
    with_decisions(portable,tmp_path)
    path=tmp_path/'atlas.zip'; manifest=archive(portable,path)
    assert manifest['format']==2
    assert len(manifest['decisions']['entries'])==3
    restored=restore_atlas(path,tmp_path/'restored')
    assert {item['receipt']['action'] for item in decision_inventory(restored)}=={'accept','reject','revise'}
    assert decision_inventory(restored)==decision_inventory(portable[0])
    assert Publications(restored).ready()==Publications(portable[0]).ready()
    for item in decision_inventory(restored):
        assert restored.closure(item['commit'])
    assert not list(restored.base.rglob('operations.sqlite3'))


def test_missing_decision_object_rejected(portable,tmp_path):
    with_decisions(portable,tmp_path)
    path=tmp_path/'original.zip';manifest=archive(portable,path)
    manifest['decisions']['entries'][0]['receipt']['candidate']='a'*40
    damaged=tmp_path/'damaged.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(damaged,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(manifest) if name=='archive.json' else source.read(name))
    with pytest.raises(ValueError,match='decision'):
        restore_atlas(damaged,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()


def test_legacy_archive_does_not_invent_decisions(portable,tmp_path):
    path=tmp_path/'new.zip';manifest=archive(portable,path)
    manifest['format']=1;manifest.pop('decisions',None)
    legacy=tmp_path/'legacy.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(legacy,'w') as target:
        for name in source.namelist():
            target.writestr(name,json.dumps(manifest) if name=='archive.json' else source.read(name))
    restored=restore_atlas(legacy,tmp_path/'restored')
    assert decision_inventory(restored)==[]
    assert Publications(restored).ready()==portable[3]


def test_restore_refuses_existing_destination(portable,tmp_path):
    path=tmp_path/'atlas.zip';archive(portable,path)
    destination=tmp_path/'existing';destination.mkdir();(destination/'keep').write_text('retained')
    with pytest.raises(ValueError,match='destination exists'): restore_atlas(path,destination)
    assert (destination/'keep').read_text()=='retained'


def test_restored_documents_replay_without_inference(portable,tmp_path):
    path=tmp_path/'atlas.zip';archive(portable,path)
    restored=restore_atlas(path,tmp_path/'restored')
    provider=EmbeddingProvider(restored.base/'assets/embedding-profile.json',restored.base/'vectors','http://127.0.0.1:1')
    result=provider.embed({'role':'document','model':portable[4]['model_namespace'],'dimensions':256,
                           'input':['Retained controlled document']})
    assert result['data'][0]['embedding']==[1.0]+[0.0]*255
    assert provider.stats['inference_calls']==0
    assert provider.stats['cache_hits']==1


def test_rebuild_maps_oldest_first_without_changing_history(portable,tmp_path):
    from knowledge_atlas.core.archive import rebuild_projections
    with_decisions(portable,tmp_path)
    store=portable[0];pubs=Publications(store);ready=pubs.ready()
    previous=pubs.read(ready)['previous_publication']
    built=[]
    class ControlledProjection:
        def build(self,content):
            built.append(content)
            return {**portable[5](content),'graph_commit':'rebuilt-'+content,'search_checkpoint':'rebuilt-'+content}
    result=rebuild_projections(store,ControlledProjection())
    mapping=json.loads((store.base/'rebuilt-projections.json').read_text())
    assert built==[pubs.read(previous)['content_commit'],pubs.read(ready)['content_commit']]
    assert result['ready']==ready
    assert mapping[ready]['graph_commit'].startswith('rebuilt-')
    assert pubs.ready()==ready
    assert pubs.read(ready)['graph_commit'].startswith('controlled-')


def test_restore_registers_only_after_rebuild(portable,tmp_path,monkeypatch):
    from knowledge_atlas.actions import archives
    from knowledge_atlas.contracts import ActionContext,ActionRequest,ActionResult,ServiceContext,AtlasError
    from knowledge_atlas.operations import database
    path=tmp_path/'atlas.zip';archive(portable,path)
    service=ServiceContext(tmp_path/'fresh-service')
    def rebuild(context):
        assert Publications(GitStore(context.atlas_path)).ready()==portable[3]
        return ActionResult(data={'ready':portable[3],'publications':1})
    monkeypatch.setattr(archives,'rebuild_restored',rebuild)
    request=ActionRequest('archive.restore',request_id=str(uuid.uuid4()),payload={'archive':str(path),'path':str(tmp_path/'restored')})
    context=ActionContext(service,request,tmp_path/'restored',tmp_path/'attempt',{})
    result=archives.restore_archive(context)
    assert result.publication==portable[3]
    assert result.data['status']=='ready'
    with database(service) as db:
        assert db.execute('SELECT path FROM atlases').fetchone()[0]==str(tmp_path/'restored')
    def unavailable(context): raise AtlasError('projection_unavailable','controlled offline',503)
    monkeypatch.setattr(archives,'rebuild_restored',unavailable)
    failed=replace(context,atlas_path=tmp_path/'unbuilt',request=replace(request,payload={**request.payload,'path':str(tmp_path/'unbuilt')}))
    with pytest.raises(AtlasError,match='projection_unavailable'): archives.restore_archive(failed)
    with database(service) as db: assert db.execute('SELECT count(*) FROM atlases').fetchone()[0]==1
    assert Publications(GitStore(tmp_path/'unbuilt')).ready()==portable[3]


def test_submitted_restore_freezes_archive_bytes(portable,tmp_path):
    from knowledge_atlas.contracts import ActionRequest,ServiceContext
    from knowledge_atlas.operations import submit
    path=tmp_path/'atlas.zip';archive(portable,path)
    original=path.read_bytes()
    request=ActionRequest('archive.restore',request_id=str(uuid.uuid4()),payload={'archive':str(path),'path':str(tmp_path/'restore')})
    service=ServiceContext(tmp_path/'service')
    operation=submit(service,request)
    path.write_bytes(b'changed after submission')
    assert (operation.evidence/'archive.zip').read_bytes()==original
    assert submit(service,request).operation_id==operation.operation_id
