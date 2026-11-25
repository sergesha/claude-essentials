"""Shared read actions retain PoC9 selection and temporal/eligibility semantics."""
from dataclasses import replace
import json
from pathlib import Path
import pytest
from acceptance_fixture import review_acceptance
from knowledge_atlas.contracts import ActionContext, ActionRequest, ServiceContext, AtlasError
from knowledge_atlas.core.git_store import GitStore
from knowledge_atlas.core.graph_stage import records_for, GraphStage
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.runtime import _assets
from knowledge_atlas.actions.reads import read_action
from test_packages import snapshot
from test_decisions import projection
from retained_semantics import proposal_records


@pytest.fixture
def published(tmp_path,monkeypatch):
    store = GitStore(tmp_path/'atlas'); base=snapshot(store)
    entries=proposal_records(store.read(base))
    candidate=propose_records(store,base,entries,actor='Fixture',method='Explicit retained fixture',
        reason='Read contract',recorded_at='2026-11-13T00:00:00Z')
    profile=json.loads((_assets()/'embedding-profile.json').read_text())
    content=review_acceptance(Publications(store), candidate,{'decision':'accept','actor':'Fixture','kind':'synthetic',
        'reason':'Read contract','accepted_at':'2026-11-13T01:00:00Z','base_publication':None},profile)
    publication=Publications(store).publish(content,lambda oid:{**projection(oid),'graph_commit':oid,
        'search_checkpoint':oid,'database':'admin/fixture'})
    docs=records_for(store.read(content),content)
    monkeypatch.setattr(GraphStage,'documents',lambda self,oid: docs if oid==content else [])
    lesson=next(row['@id'] for row in docs if row['kind']=='lesson')
    service=ServiceContext(tmp_path/'service',configuration={'graph':{'url':'http://127.0.0.1:1'}})
    return store,publication,content,lesson,service,docs


def read_context(published,action,payload,**selectors):
    store,_,_,_,service,_=published
    return ActionContext(service,ActionRequest(action,payload=payload,**selectors),store.base,service.service_root,service.configuration)


def test_unknown_validity_is_not_acceptance_time(published):
    result=read_action(read_context(published,'knowledge.apply',{'record_id':published[3],
        'context':{'feedstock':'dried','enclosure':'closed','ambient_c':22}},valid_at='2026-11-13T02:00:00Z'))
    assert result.publication==published[1]
    assert result.data['status']=='conditional'
    assert 'valid_time' in result.data['unknowns']


def test_unavailable_historical_projection_is_not_fallback(published,monkeypatch):
    def unavailable(*_): raise RuntimeError('selected projection unavailable')
    monkeypatch.setattr(GraphStage,'documents',unavailable)
    with pytest.raises(AtlasError) as caught:
        read_action(read_context(published,'knowledge.apply',{'record_id':published[3],'context':{}},publication=published[1]))
    assert caught.value.code=='projection_unavailable'
    assert caught.value.status==503


def test_empty_eligibility_is_not_global_search(published,monkeypatch):
    def unexpected(*_): pytest.fail('empty eligibility reached native search')
    monkeypatch.setattr(GraphStage,'documents',unexpected)
    result=read_action(read_context(published,'search',{'query':'warping','eligible':[]}))
    assert result.data['status']=='no_eligible_records'
    assert result.data['hits']==[]


def test_evidence_history_and_diff_keep_selected_identity(published):
    result=read_action(read_context(published,'evidence.read',{'record_id':published[3]}))
    assert result.data['record']['@id']==published[3]
    history=read_action(read_context(published,'history',{'record_id':published[3]}))
    assert history.data['history'][0]['publication_commit']==published[1]
    diff=read_action(read_context(published,'diff',{'before':published[1],'after':published[1]}))
    assert diff.data['changes']==[]


def test_optional_explanation_cannot_replace_deterministic_assessment(published,monkeypatch):
    from knowledge_atlas.core import model_config
    class ExplanationModel:
        def __init__(self,**_): pass
        def __call__(self,request):
            assert request['operation']=='grounded_answer'
            assert request['assessment']['status']=='conditional'
            return {'answer':'The trial conditions are incomplete.','citations':[published[3]],'unknowns':['feedstock'],
                    'status':'supported'}
    monkeypatch.setattr(model_config,'ConfiguredModels',ExplanationModel)
    result=read_action(read_context(published,'knowledge.apply',{'record_id':published[3],
        'context':{'enclosure':'closed','ambient_c':22},'explain':True}))
    assert result.data['status']=='conditional'
    assert result.data['explanation']['answer']=='The trial conditions are incomplete.'
    assert result.data['explanation']['citations']==[published[3]]


def test_expansion_is_bounded_and_identifies_publication(published):
    result=read_action(read_context(published,'evidence.read',{'record_id':published[3],'expand':True,'depth':1,'limit':1}))
    assert result.data['expansion']['ids']==[published[3]]
    assert result.data['expansion']['truncated'] is True
    assert result.data['expansion']['publication_commit']==published[1]


def test_publication_history_does_not_transport_embedding_payloads(published, monkeypatch):
    # Large native projection artifacts must not make a timeline RPC unbounded.
    original = Publications.read
    loaded = []
    def with_artifacts(self, oid):
        value = original(self, oid)
        value = {**value, 'vector_artifacts': {'vectors': [0.125] * 100_000},
                 'native_export_excluded_ids': ['Record/excluded'] * 10_000}
        loaded.append(value)
        return value
    monkeypatch.setattr(Publications, 'read', with_artifacts)
    result = read_action(read_context(published, 'history', {}))
    assert result.publication == published[1]
    entry = result.data['history'][0]
    assert entry['publication_commit'] == published[1]
    assert entry['content_commit'] == published[2]
    assert entry['acceptance_kind'] == 'synthetic'
    assert entry['previous_publication'] is None
    assert len(json.dumps(result.data)) < 10_000
    assert 'vector_artifacts' not in entry
    assert 'native_export_excluded_ids' not in entry
    assert loaded
    assert all(len(value['vector_artifacts']['vectors']) == 100_000 for value in loaded)
    assert all(len(value['native_export_excluded_ids']) == 10_000 for value in loaded)



@pytest.fixture
def registered_history(published):
    from knowledge_atlas.operations import register_atlas

    store, first, content, _, service, _ = published
    pubs = Publications(store)
    atlas_id = store.read(content)['manifest']['package_id']
    binding = register_atlas(service, atlas_id, store.base)
    candidate = propose_records(store, content, [], actor='Fixture', method='Explicit retained fixture',
        reason='Advance READY', recorded_at='2026-11-14T00:00:00Z', base_publication=first)
    profile = json.loads((_assets() / 'embedding-profile.json').read_text())
    accepted = review_acceptance(pubs, candidate, {'decision': 'accept', 'actor': 'Fixture', 'kind': 'synthetic',
        'reason': 'Advance READY', 'accepted_at': '2026-11-14T01:00:00Z', 'base_publication': first}, profile)
    ready = pubs.publish(accepted, lambda oid: {
        **projection(oid), 'graph_commit': oid, 'search_checkpoint': oid, 'database': binding.graph_database})
    return binding, ready


def set_rebuilt_database(store, publication, name):
    receipt = Publications(store).read(publication)
    (store.base / 'rebuilt-projections.json').write_text(
        json.dumps({publication: {**receipt, 'database': name}}))


@pytest.mark.parametrize(('selection', 'owner'), [('ready', 'foreign'), ('historical', 'foreign'),
                                                 ('historical', 'unverified')])
def test_selected_read_rejects_foreign_or_unverified_graph(published, registered_history,
                                                          selection, owner, monkeypatch):
    from knowledge_atlas.http_api import invoke
    from knowledge_atlas.operations import register_atlas

    store, first, _, _, service, docs = published
    binding, ready = registered_history
    database = 'admin/unverified'
    if owner == 'foreign':
        other = GitStore(store.base.with_name('other'))
        identity = other.read(snapshot(other))['manifest']['package_id']
        database = register_atlas(service, identity, other.base).graph_database
    selected = first if selection == 'historical' else ready
    set_rebuilt_database(store, selected, database)

    queried = []

    def documents(graph, commit):
        queried.append((graph.database, commit))
        return docs

    monkeypatch.setattr(GraphStage, 'documents', documents)
    request = ActionRequest('search', atlas_id=binding.atlas_id,
                            publication=selected if selection == 'historical' else None,
                            payload={'query': 'warping', 'mode': 'vector'})
    with pytest.raises(AtlasError) as failure:
        invoke(service, request)
    assert (failure.value.code, failure.value.status, queried) == ('graph_atlas_conflict', 409, [])


def test_historical_read_retains_project_owned_graph(published, registered_history, monkeypatch):
    from knowledge_atlas.http_api import invoke
    from knowledge_atlas.operations import database

    store, first, content, lesson, service, docs = published
    binding, ready = registered_history
    old_database = binding.graph_database
    set_rebuilt_database(store, first, old_database)
    with database(service) as db:
        db.execute('INSERT INTO projection_owners VALUES (?,?,?)',
                   ('http://127.0.0.1:1', old_database, binding.atlas_id))

    queried = []

    def documents(graph, commit):
        queried.append((graph.database, commit))
        return docs

    monkeypatch.setattr(GraphStage, 'documents', documents)
    status, result = invoke(service, ActionRequest('knowledge.apply', atlas_id=binding.atlas_id,
        publication=first, payload={'record_id': lesson, 'context': {}}))
    assert status == 200
    assert result.publication == first != ready
    assert result.data['status'] == 'conditional'
    assert queried == [(old_database, content)]

@pytest.mark.parametrize('action', ['history', 'wiki.pages'])
def test_selected_reads_reject_a_missing_acceptance_decision(published, action):
    from knowledge_atlas.core.decision_receipts import decision_inventory
    from knowledge_atlas.core.git_store import git
    store, publication = published[:2]
    entry, = decision_inventory(store)
    git(store.root, 'update-ref', '-d', entry['ref'])
    with pytest.raises(AtlasError, match='publication_unavailable.*decision'):
        read_action(read_context(published, action, {}, publication=publication))


def test_history_refuses_retired_effective_acceptance_time(published):
    from knowledge_atlas.core.git_store import git
    from knowledge_atlas.core.publication import commit_files
    store, publication, content = published[:3]
    receipt = Publications(store).read(publication)
    substituted = {**receipt, 'effective_accepted_at': '2099-01-01T00:00:00Z'}
    broken = commit_files(store.root, content, {'publication.json': substituted}, [content], 'Obsolete cutoff')
    git(store.root, 'update-ref', Publications.ref, broken, publication)
    with pytest.raises(AtlasError, match='publication_unavailable.*effective_accepted_at'):
        read_action(read_context(published, 'history', {}))
