"""Publication reads must not silently switch versions or invent citations."""
import json
import pytest
from test_reads import published, read_context
from test_decisions import projection
from knowledge_atlas.actions.reads import read_action
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.reader import AtlasReader
from knowledge_atlas.contracts import AtlasError


def test_pages_use_selected_publication_after_ready_advances(published):
    store, first, content, *_ = published
    original=store.read(content)
    wiki=original['wiki']; wiki['pages'][0]['content']='Changed later page.'
    from knowledge_atlas.core.feedback import revise_compilation
    candidate=revise_compilation(store,content,[],wiki,original['extraction'],{},actor='Fixture',
        method='Fixture',reason='Changed page',recorded_at='2026-11-14T00:00:00Z',base_publication=first)
    from knowledge_atlas.runtime import _assets
    accepted=Publications(store).review(candidate,{'decision':'accept','actor':'Test','kind':'synthetic',
        'reason':'new version','accepted_at':'2026-11-14T01:00:00Z','base_publication':first},
        json.loads((_assets()/'embedding-profile.json').read_text()))
    Publications(store).publish(accepted,projection)
    pages=read_action(read_context(published,'wiki.pages',{},publication=first))
    page=pages.data['pages'][0]
    result=read_action(read_context(published,'wiki.read',{'page_id':page['id']},publication=first))
    assert result.publication==first
    assert result.data['page']['content']!='Changed later page.'
    assert pages.data['accepted_at']=='2026-11-13T01:00:00Z'
    assert pages.data['recorded_at']=='2026-11-13T00:00:00Z'
    with pytest.raises(AtlasError):
        read_action(read_context(published,'wiki.read',{'page_id':'../../secret'},publication=first))


def test_question_without_evidence_does_not_invoke_model(published,monkeypatch):
    monkeypatch.setattr(AtlasReader,'search',lambda self,*a,**k:{'status':'no_matches','hits':[]})
    from knowledge_atlas.core import model_config
    monkeypatch.setattr(model_config,'ConfiguredModels',lambda **k:pytest.fail('no evidence must not call provider'))
    result=read_action(read_context(published,'question.ask',{'question':'unknown'}))
    assert result.data['status']=='insufficient_evidence'
    assert result.data['citations']==[]
    assert result.publication==published[1]


@pytest.mark.parametrize('foreign',[False,True])
def test_question_citations_are_bound_to_supplied_selected_evidence(published,monkeypatch,foreign):
    rid=published[3]
    monkeypatch.setattr(AtlasReader,'search',lambda self,*a,**k:{'status':'matches','hits':[{'record':self.records[rid]}]})
    from knowledge_atlas.core import model_config
    class Model:
        def __init__(self,**kwargs):pass
        def __call__(self,request):
            assert request['publication']==published[1]
            assert request['evidence'][0]['record_id']==rid
            return {'coverage':'answered','answer':'A bounded answer.','citations':['Record/foreign' if foreign else rid],'unknowns':[]}
    monkeypatch.setattr(model_config,'ConfiguredModels',Model)
    if foreign:
        with pytest.raises(AtlasError) as error:read_action(read_context(published,'question.ask',{'question':'What is supported?'}))
        assert error.value.code=='invalid_model_response'
    else:
        r=read_action(read_context(published,'question.ask',{'question':'What is supported?'}))
        assert r.data['citations']==[rid]
        assert r.data['answer']=='A bounded answer.'
        assert r.data['evidence'][0]['record_id']==rid


def test_question_failure_does_not_become_missing_evidence(published,monkeypatch):
    monkeypatch.setattr(AtlasReader,'search',lambda self,*a,**k:{'status':'technical_error','reason':'projection unavailable'})
    with pytest.raises(AtlasError) as error:read_action(read_context(published,'question.ask',{'question':'Anything?'}))
    assert error.value.code=='projection_unavailable'


def test_question_marks_truncated_evidence_and_preserves_bound(published,monkeypatch):
    rid=published[3]
    monkeypatch.setattr(AtlasReader,'search',lambda self,*a,**k:{'status':'matches','hits':[{'record':self.records[rid]}]})
    original=AtlasReader.retrieve_evidence
    def retrieve(self,key):
        result=original(self,key)
        result['record']={**result['record'],'text':'x'*100000,'payload':'y'*100000}
        return result
    monkeypatch.setattr(AtlasReader,'retrieve_evidence',retrieve)
    from knowledge_atlas.core import model_config
    class Model:
        def __init__(self,**kwargs):pass
        def __call__(self,request):
            assert request['truncated'] is True
            assert sum(len(x['text'])+len(x['payload']) for x in request['evidence'])<=48000
            return {'coverage':'partial','answer':'Partial evidence only.','citations':[rid],'unknowns':['Evidence was truncated.']}
    monkeypatch.setattr(model_config,'ConfiguredModels',Model)
    result=read_action(read_context(published,'question.ask',{'question':'What is known?'}))
    assert result.data['bounds']['truncated'] is True


def test_composed_pages_retain_distinct_canonical_ids(published,tmp_path):
    from knowledge_atlas.core.git_store import GitStore
    from knowledge_atlas.runtime import _assets
    store,first,content,*_=published
    snapshot=store.read(content)
    child=GitStore(tmp_path/'child')
    child_commit=child.snapshot(snapshot['wiki'],snapshot['extraction'],{},recorded_at='2026-11-14T00:00:00Z')['commit']
    candidate=store.pin_package(content,child,child_commit,mount='shared/child',recorded_at='2026-11-14T00:00:00Z',base_publication=first)
    accepted=Publications(store).review(candidate,{'decision':'accept','actor':'Test','kind':'synthetic',
        'reason':'composition','accepted_at':'2026-11-14T01:00:00Z','base_publication':first},
        json.loads((_assets()/'embedding-profile.json').read_text()))
    selected=Publications(store).publish(accepted,lambda oid:{**projection(oid),'graph_commit':oid,'search_checkpoint':oid,'database':'admin/fixture'})
    pages=read_action(read_context(published,'wiki.pages',{},publication=selected)).data['pages']
    assert len(pages)==2*len(snapshot['wiki']['pages'])
    assert len({p['id'] for p in pages})==len(pages)
    for page in pages:
        result=read_action(read_context(published,'wiki.read',{'page_id':page['id']},publication=selected))
        assert result.publication==selected and result.data['page']['id']==page['id']

def test_retrieved_evidence_can_still_be_insufficient(published, monkeypatch):
    from knowledge_atlas.core import model_config
    rid = published[3]
    monkeypatch.setattr(AtlasReader, 'search', lambda self, *a, **k: {
        'hits': [{'record': self.records[rid]}]})
    monkeypatch.setattr(model_config, 'ConfiguredModels', lambda **k: lambda request: {
        'coverage': 'insufficient', 'answer': 'The publication does not cover this.',
        'unknowns': ['Material strength'], 'citations': [rid]})
    result = read_action(read_context(published, 'question.ask', {'question': 'Material strength?'}))
    assert result.data['status'] == 'insufficient_evidence'
    assert result.data['citations'] == [rid]
