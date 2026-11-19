from dataclasses import replace
import json
import uuid
from knowledge_atlas.contracts import ActionRequest
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.git_store import git
from knowledge_atlas.core.graph_stage import records_for
from knowledge_atlas.core.applicability import assess
from knowledge_atlas.actions.feedback import feedback_action
from knowledge_atlas.actions.decisions import decide_candidate
from test_reads import published,read_context
from test_decisions import context_for,projection


def test_feedback_keeps_applied_publication(published,tmp_path,monkeypatch):
    from knowledge_atlas.actions import decisions
    store,first,content,lesson,service,_=published
    monkeypatch.setattr(decisions,'build_projection',lambda context,oid:{**projection(oid),'database':'admin/fixture'})
    entries=store.read(content)['knowledge_records']
    change={**entries[2],'text':'Changed current lesson with retained provenance.'}
    context=read_context(published,'knowledge.revise',{'record_id':lesson,'entries':[change],
        'actor':'Fixture','reason':'Correction','recorded_at':'2026-11-13T02:00:00Z'})
    context=replace(context,request=replace(context.request,request_id=str(uuid.uuid4()),expected_publication=first))
    revised=feedback_action(context)
    second=decide_candidate(context_for(tmp_path,store,revised.data['candidate'],'accept',first)).publication
    raw='{"case_id":"late-report","description":"Application failed in this context.","context":{"enclosure":"open"}}'
    request=ActionRequest('feedback.record',request_id=str(uuid.uuid4()),expected_publication=second,payload={
        'record_id':lesson,'applied_publication':first,'raw_report':raw,'actor':'Fixture','recorded_at':'2026-11-13T03:00:00Z'})
    result=feedback_action(replace(context,request=request))
    outcome=next(row for row in store.read(result.data['candidate'])['knowledge_records'] if row['kind']=='outcome')
    assert outcome['application_publication']==first
    assert outcome['raw_report']==raw
    assert outcome['provenance'][0]['source_commit']==content
    assert outcome['provenance'][0]['quote']==entries[2]['text']
    assert Publications(store).ready()==second


def test_recursive_withdrawal_preserves_independent_support(published,tmp_path,monkeypatch):
    from knowledge_atlas.actions import decisions
    store,first,content,lesson,service,_=published
    monkeypatch.setattr(decisions,'build_projection',lambda context,oid:{**projection(oid),'database':'admin/fixture'})
    entries=store.read(content)['knowledge_records']
    context=read_context(published,'knowledge.revise',{'record_id':lesson,
        'entries':[dict(entries[0],state='withdrawn',reason='Withdrawn trial')],
        'actor':'Fixture','reason':'Withdraw trial','recorded_at':'2026-11-13T02:00:00Z'})
    context=replace(context,request=replace(context.request,request_id=str(uuid.uuid4()),expected_publication=first))
    result=feedback_action(context)
    accepted=decide_candidate(context_for(tmp_path,store,result.data['candidate'],'accept',first))
    rows={r['@id']:json.loads(r['payload']) for r in records_for(store.read(accepted.data['accepted_content']),accepted.data['accepted_content'])
          if r['@id'].startswith('Record/knowledge-')}
    assessment=assess(rows,lesson,{'feedstock':'dried','enclosure':'closed','ambient_c':22})
    assert assessment['status']=='supported'
    assert assessment['independent_episodes']==['P-22']
    assert 'impact' in result.data
    assert store.read(content)['knowledge_records'][0]['state']=='active'


def test_mixed_revision_inputs_are_not_silently_ignored(published):
    import pytest
    from knowledge_atlas.contracts import AtlasError
    context=read_context(published,'knowledge.revise',{'record_id':published[3],'entries':[],
        'instructions':'Correct the source page too.','actor':'Fixture','kind':'synthetic',
        'reason':'Mixed correction','recorded_at':'2026-11-13T02:00:00Z'})
    context=replace(context,request=replace(context.request,request_id=str(uuid.uuid4()),expected_publication=published[1]))
    with pytest.raises(AtlasError,match='choose structured entries or native correction'):
        feedback_action(context)


def test_revision_requires_a_retained_target(published):
    import pytest
    from knowledge_atlas.contracts import AtlasError
    store=published[0]
    before=git(store.root,'for-each-ref','refs/poc9/proposals/')
    context=read_context(published,'knowledge.revise',{'record_id':'Record/missing','entries':[],
        'actor':'Fixture','reason':'Unknown target','recorded_at':'2026-11-13T02:00:00Z'})
    context=replace(context,request=replace(context.request,request_id=str(uuid.uuid4()),expected_publication=published[1]))
    with pytest.raises(AtlasError) as caught: feedback_action(context)
    assert caught.value.code=='record_not_found'
    assert git(store.root,'for-each-ref','refs/poc9/proposals/')==before
