"""Service composition over PoC9 exact pins and retained printlab source material."""
import os
import json
from pathlib import Path
import uuid
import pytest
from knowledge_atlas.contracts import ActionRequest, ActionContext, ServiceContext, AtlasError
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.graph_stage import records_for, key
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.applicability import assess
from knowledge_atlas.actions.packages import inspect_packages
from knowledge_atlas.actions.decisions import decide_candidate
from test_decisions import context_for, projection


def snapshot(store, version=0):
    wiki = json.loads((Path(__file__).parent/'fixtures/evidence/wiki-initial-final.json').read_text())
    return store.snapshot(wiki, {'entities':[], 'statements':[]}, {'version':version},
                          recorded_at='2026-11-13T00:00:00Z')['commit']


def publish(store, candidate, build=projection):
    from knowledge_atlas.runtime import _assets
    pubs = Publications(store)
    content = pubs.review(candidate, {'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture',
        'reason': 'Review child layout', 'accepted_at': '2026-11-13T02:00:00Z',
        'base_publication': pubs.ready()}, json.loads((_assets()/'embedding-profile.json').read_text()))
    return pubs.publish(content, build)


def selection(parent, child, publication, mount='shared/child'):
    content = Publications(child).read(publication)['content_commit']
    return {'publication':publication, 'commit':content, 'mount':mount,
            'url':os.path.relpath(child.root, parent.root),
            'package_id':child.read(content)['manifest']['package_id']}


def layout(parent, *includes):
    from knowledge_atlas.core.composition import configure_composition
    pubs = Publications(parent)
    base = pubs.read(pubs.ready())['content_commit'] if pubs.ready() else git(parent.root, 'rev-parse', 'HEAD')
    return configure_composition(parent, base, includes, base_publication=pubs.ready(),
                                 recorded_at='2026-11-13T01:00:00Z')


def test_child_advance_does_not_change_parent(tmp_path):
    parent, child = GitStore(tmp_path/'parent'), GitStore(tmp_path/'child')
    snapshot(parent)
    first = publish(child, snapshot(child))
    selected = selection(parent, child, first)
    candidate = layout(parent, selected)
    original = parent.read(candidate)
    publish(child, layout(child))
    assert parent.read(candidate) == original
    assert original['included_packages'][0]['commit'] == selected['commit']
    assert Publications(parent).ready() is None


def test_pin_and_rollback_require_review(tmp_path):
    parent, child = GitStore(tmp_path/'parent'), GitStore(tmp_path/'child')
    snapshot(parent)
    first = publish(child, snapshot(child))
    second = publish(child, layout(child))
    original = selection(parent, child, first)
    composed = layout(parent, original)
    published = publish(parent, composed)
    updated = layout(parent, selection(parent, child, second))
    assert Publications(parent).ready() == published
    next_publication = publish(parent, updated)
    rollback = layout(parent, original)
    assert Publications(parent).ready() == next_publication
    for state in (rollback, composed):
        assert parent.read(state)['included_packages'][0]['commit'] == original['commit']


def test_diamond_inclusion_has_single_evidence_weight(tmp_path):
    root,left,right,shared = [GitStore(tmp_path/name) for name in ('root','left','right','shared')]
    for store in (root,left,right): snapshot(store)
    shared_pub = publish(shared, snapshot(shared))
    branches = []
    for child in (left,right):
        pub = publish(child, layout(child, selection(child, shared, shared_pub)))
        branches.append(selection(root, child, pub, 'shared/'+child.base.name))
    composed = layout(root, *branches)
    shared_oid = Publications(shared).read(shared_pub)['content_commit']
    shared_id = shared.read(shared_oid)['manifest']['package_id']
    docs = [record for record in records_for(root.read(composed),composed)
            if json.loads(record['payload']).get('package_id') == shared_id]
    assert len(docs) == len(records_for(shared.read(shared_oid),shared_oid))
    assert len({r['@id'] for r in docs}) == len(docs)
    assert json.loads(docs[0]['payload'])['mounts'] == ['shared/left/shared/child','shared/right/shared/child']


def test_cross_package_withdrawal_preserves_alternative(tmp_path):
    from knowledge_atlas.runtime import _assets
    parent,child = GitStore(tmp_path/'parent'),GitStore(tmp_path/'child')
    parent_base,child_base = snapshot(parent),snapshot(child)
    profile = json.loads((_assets()/'embedding-profile.json').read_text())
    def propose(store,base,entries):
        return propose_records(store,base,entries,actor='Synthetic fixture',method='Explicit curation',
                               reason='Cross-package grounds',recorded_at='2026-11-13T01:00:00Z',base_publication=Publications(store).ready())
    def accept(store,candidate):
        return Publications(store).review(candidate,{'decision':'accept','kind':'synthetic','actor':'Fixture',
            'reason':'Controlled grounds check','accepted_at':'2026-11-13T02:00:00Z','base_publication':Publications(store).ready()},profile)
    quote = child.read(child_base)['sources']['review.txt'].decode().split('\n')[0]
    provenance = [{'record_id':'Record/'+key('source','review.txt'),'start':0,'end':len(quote),'quote':quote}]
    entries = [{'id':name,'kind':'episode','text':'Retained synthetic trial '+name,'episode_id':name,'provenance':provenance}
               for name in ('P-14','P-22')]
    child_content = accept(child,propose(child,child_base,entries))
    package_id = child.read(child_content)['manifest']['package_id']
    ids = ['Record/'+key('knowledge',package_id+':'+name) for name in ('P-14','P-22')]
    child_pub = Publications(child).publish(child_content, projection)
    composed = layout(parent, selection(parent, child, child_pub))
    lessons = [{'id':'joint','kind':'lesson','text':'Joint grounds','support_sets':[ids],'provenance':provenance},
               {'id':'alternative','kind':'lesson','text':'Independent alternative','support_sets':[[rid] for rid in ids],'provenance':provenance}]
    content = accept(parent,propose(parent,composed,lessons))
    Publications(parent).publish(content, projection)
    changed = accept(child,propose(child,child_content,[dict(entries[0],state='withdrawn',reason='Withdrawn trial')]))
    new_pin = layout(parent, selection(parent, child, Publications(child).publish(changed, projection)))
    revised = accept(parent,new_pin)
    records = {r['@id']:json.loads(r['payload']) for r in records_for(parent.read(revised),revised)
               if r['@id'].startswith('Record/knowledge-')}
    by_local = {r['local_id']:r for r in records.values()}
    assert assess(records,by_local['joint']['id'],{})['status'] in ('insufficient','needs_review')
    alternate = assess(records,by_local['alternative']['id'],{})
    assert alternate['status'] == 'supported'
    assert alternate['independent_episodes'] == ['P-22']


def test_inspection_pins_selected_publication_after_relocation(tmp_path, monkeypatch):
    from knowledge_atlas.actions import decisions
    monkeypatch.setattr(decisions,'build_projection',lambda context,content:{**projection(content),'database':'admin/fixture'})
    parent,child = GitStore(tmp_path/'parent'),GitStore(tmp_path/'child')
    snapshot(parent); first = snapshot(child)
    wrapper = publish(child, first)
    from knowledge_atlas.project_registry import register_project
    selected_child = selection(parent, child, wrapper)
    register_project(ServiceContext(tmp_path/'service'), selected_child['package_id'], child.base)
    candidate = layout(parent, selected_child)
    publication = decide_candidate(context_for(tmp_path,parent,candidate,'accept')).publication
    package_id = child.read(first)['manifest']['package_id']
    moved = tmp_path/'moved'; child.base.rename(moved)
    (parent.base/'package-locations.json').write_text(json.dumps({package_id:str(moved/'atlas')}))
    request = ActionRequest('packages.inspect',publication=publication)
    result = inspect_packages(ActionContext(ServiceContext(tmp_path/'service'),request,parent.base,tmp_path/'evidence',{}))
    selected = next(node for node in result.data['packages'] if node['package_id']==package_id)
    assert selected['commit'] == selected_child['commit']
    assert selected['mounts'] == ['shared/child']
    assert selected['inclusions'] == [{'mount':'shared/child', 'publication':wrapper}]
    assert selected['path'] == str(moved/'atlas')
    assert result.publication == publication
    json.dumps(result.data)


def test_empty_atlas_can_include_its_first_package(tmp_path):
    from knowledge_atlas.actions.atlas import create_atlas
    parent,child = GitStore(tmp_path/'parent'),GitStore(tmp_path/'child')
    context = ActionContext(ServiceContext(tmp_path/'service'), ActionRequest('atlas.create'),
                            parent.base,tmp_path/'evidence',{})
    create_atlas(context)
    child_oid = snapshot(child)
    candidate = layout(parent, selection(parent, child, publish(child, child_oid)))
    composed = parent.read(candidate)
    assert len(records_for(composed,candidate)) == len(records_for(child.read(child_oid),child_oid))
    assert Publications(parent).ready() is None


def test_unsupported_pin_has_no_durable_effect(tmp_path):
    from knowledge_atlas.operations import database, submit
    from knowledge_atlas.project_registry import register_project
    parent, child = GitStore(tmp_path/'parent'), GitStore(tmp_path/'child')
    base, selected = snapshot(parent), snapshot(child)
    service = ServiceContext(tmp_path/'service')
    parent_id = parent.read(base)['manifest']['package_id']
    register_project(service, parent_id, parent.base)
    refs = [git(store.root, 'show-ref') for store in (parent, child)]
    request = ActionRequest('packages.pin', atlas_id=parent_id, request_id=str(uuid.uuid4()),
        payload={'path':str(child.base), 'package_id':child.read(selected)['manifest']['package_id'],
                 'commit':selected, 'mount':'child', 'recorded_at':'2026-11-13T00:00:00Z'})
    with pytest.raises(AtlasError, match='unsupported_action'):
        submit(service, request)
    assert refs == [git(store.root, 'show-ref') for store in (parent, child)]
    with database(service) as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
