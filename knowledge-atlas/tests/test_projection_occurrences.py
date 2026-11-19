"""Duplicate native statement occurrences retain their separate alignment rows."""
import copy
import json
from knowledge_atlas.core.graph_stage import records_for, key
from knowledge_atlas.core.relations import core_catalog, digest


def snapshot(count):
    statement={'subject':'the archive','object':'public release','value':None,
        'phrase':'has not approved','quote':'The archive has not approved public release.',
        'qualifiers':[],'when':None,'ended':None}
    extraction={'entities':[{'name':'the archive','kind':'archive','named':False},
        {'name':'public release','kind':'release','named':False}],
        'statements':[copy.deepcopy(statement) for _ in range(count)]}
    rows=[]
    for index in range(count):
        binding={'id':index,'key':'does_not_adopt','direction':'forward','marks':None}
        rows.append({**statement,'index':index,'status':'resolved','type_id':'does_not_adopt',
            'polarity':'positive','reason':'Native phrase_align binding',
            'details':{'native_binding':binding},'native_binding':binding})
    return {'manifest':{'package_id':'fixture','recorded_at':'2026-11-13T00:00:00Z','status':'candidate'},
        'wiki':{'sources':[],'pages':[],'references':[]},'sources':{},'extraction':extraction,
        'relations':{'version':1,'extraction_digest':digest(extraction),'catalog':core_catalog(),
            'relations':rows,'decisions':[]}}


def test_duplicate_statement_alignments_project_without_losing_native_occurrences():
    data=snapshot(2)
    records=records_for(data,'a'*40)
    assert len({r['@id'] for r in records})==len(records)
    statements=[r for r in records if r['kind']=='statement']
    relations=[r for r in records if r['kind']=='relation']
    assert len(statements)==1
    assert len(relations)==2
    assert {json.loads(r['payload'])['index'] for r in relations}=={0,1}
    assert all(statements[0]['@id'] in r['links'] for r in relations)
    assert all(json.loads(r['payload'])['type_id']=='does_not_adopt' for r in relations)
    assert records_for(data,'a'*40)==records


def test_unique_statement_keeps_retained_poc9_relation_identity():
    records=records_for(snapshot(1),'a'*40)
    statement=next(r for r in records if r['kind']=='statement')
    relation=next(r for r in records if r['kind']=='relation')
    assert relation['@id']=='Record/'+key('relation',statement['@id'])
