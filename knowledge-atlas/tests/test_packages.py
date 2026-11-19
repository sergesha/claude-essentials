"""Service composition over PoC9 exact pins and retained printlab source material."""
from dataclasses import replace
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
from knowledge_atlas.actions.packages import pin_package, inspect_packages
from knowledge_atlas.actions.decisions import decide_candidate
from test_decisions import context_for, projection


def snapshot(store, version=0):
    wiki = json.loads((Path(__file__).parent/'fixtures/evidence/wiki-initial-final.json').read_text())
    return store.snapshot(wiki, {'entities':[], 'statements':[]}, {'version':version},
                          recorded_at='2026-11-13T00:00:00Z')['commit']


def pin(tmp_path, parent, child, commit, mount='shared/child', expected=None, base=None):
    payload = {'path':str(child.base), 'package_id':child.read(commit)['manifest']['package_id'],
               'commit':commit, 'mount':mount, 'recorded_at':'2026-11-13T01:00:00Z'}
    if base: payload['candidate'] = base
    request = ActionRequest('packages.pin', request_id=str(uuid.uuid4()), expected_publication=expected, payload=payload)
    return pin_package(ActionContext(ServiceContext(tmp_path/'service'), request, parent.base, tmp_path/'evidence', {}))


def test_child_advance_does_not_change_parent(tmp_path):
    parent, child = GitStore(tmp_path/'parent'), GitStore(tmp_path/'child')
    base, first = snapshot(parent), snapshot(child)
    candidate = pin(tmp_path,parent,child,first).data['candidate']
    original = parent.read(candidate)
    snapshot(child, 2)
    assert parent.read(candidate) == original
    assert original['included_packages'][0]['commit'] == first
    assert Publications(parent).ready() is None


def test_pin_and_rollback_require_review(tmp_path, monkeypatch):
    from knowledge_atlas.actions import decisions
    monkeypatch.setattr(decisions, 'build_projection', lambda context,content:projection(content))
    parent, child = GitStore(tmp_path/'parent'), GitStore(tmp_path/'child')
    snapshot(parent); first = snapshot(child); second = snapshot(child, 2)
    composed = pin(tmp_path,parent,child,first).data['candidate']
    published = decide_candidate(context_for(tmp_path,parent,composed,'accept')).publication
    updated = pin(tmp_path,parent,child,second,expected=published).data['candidate']
    assert Publications(parent).ready() == published
    next_publication = decide_candidate(context_for(tmp_path,parent,updated,'accept',published)).publication
    rollback = pin(tmp_path,parent,child,first,expected=next_publication).data['candidate']
    assert Publications(parent).ready() == next_publication
    assert parent.read(rollback)['included_packages'][0]['commit'] == first
    assert parent.read(composed)['included_packages'][0]['commit'] == first


def test_diamond_inclusion_has_single_evidence_weight(tmp_path):
    root,left,right,shared = [GitStore(tmp_path/name) for name in ('root','left','right','shared')]
    for store in (root,left,right): snapshot(store)
    shared_oid = snapshot(shared)
    left_oid = pin(tmp_path,left,shared,shared_oid).data['candidate']
    right_oid = pin(tmp_path,right,shared,shared_oid).data['candidate']
    first = pin(tmp_path,root,left,left_oid,mount='shared/left').data['candidate']
    composed = pin(tmp_path,root,right,right_oid,mount='shared/right',base=first).data['candidate']
    shared_id = shared.read(shared_oid)['manifest']['package_id']
    docs = [record for record in records_for(root.read(composed),composed)
            if json.loads(record['payload']).get('package_id') == shared_id]
    assert len(docs) == len(records_for(shared.read(shared_oid),shared_oid))
    assert len({r['@id'] for r in docs}) == len(docs)
    assert json.loads(docs[0]['payload'])['mounts'] == ['shared/left/shared/child','shared/right/shared/child']


def test_invalid_closure_is_explicit(tmp_path):
    parent,child = GitStore(tmp_path/'parent'),GitStore(tmp_path/'child')
    base,first = snapshot(parent),snapshot(child)
    composed = pin(tmp_path,parent,child,first).data['candidate']
    second = snapshot(child,2)
    before = git(parent.root,'for-each-ref','refs/poc9/proposals/')
    with pytest.raises(AtlasError,match='conflicting pins'):
        pin(tmp_path,parent,child,second,mount='shared/other',base=composed)
    assert git(parent.root,'for-each-ref','refs/poc9/proposals/') == before
    with pytest.raises(AtlasError,match='overlapping'):
        pin(tmp_path,parent,child,first,mount='shared/child/nested',base=composed)
    with pytest.raises(AtlasError,match='cycle'):
        pin(tmp_path,child,parent,composed)
    request = ActionRequest('packages.pin',request_id=str(uuid.uuid4()),payload={
        'path':str(tmp_path/'missing'),'package_id':str(uuid.uuid4()),'commit':first,
        'mount':'shared/missing','recorded_at':'2026-11-13T01:00:00Z'})
    with pytest.raises(AtlasError,match='invalid_package'):
        pin_package(ActionContext(ServiceContext(tmp_path/'service'),request,parent.base,tmp_path/'evidence',{}))


def test_cross_package_withdrawal_preserves_alternative(tmp_path):
    from knowledge_atlas.runtime import _assets
    parent,child = GitStore(tmp_path/'parent'),GitStore(tmp_path/'child')
    parent_base,child_base = snapshot(parent),snapshot(child)
    profile = json.loads((_assets()/'embedding-profile.json').read_text())
    def propose(store,base,entries):
        return propose_records(store,base,entries,actor='Synthetic fixture',method='Explicit curation',
                               reason='Cross-package grounds',recorded_at='2026-11-13T01:00:00Z')
    def accept(store,candidate):
        return Publications(store).review(candidate,{'decision':'accept','kind':'synthetic','actor':'Fixture',
            'reason':'Controlled grounds check','accepted_at':'2026-11-13T02:00:00Z','base_publication':None},profile)
    quote = child.read(child_base)['sources']['review.txt'].decode().split('\n')[0]
    provenance = [{'record_id':'Record/'+key('source','review.txt'),'start':0,'end':len(quote),'quote':quote}]
    entries = [{'id':name,'kind':'episode','text':'Retained synthetic trial '+name,'episode_id':name,'provenance':provenance}
               for name in ('P-14','P-22')]
    child_content = accept(child,propose(child,child_base,entries))
    package_id = child.read(child_content)['manifest']['package_id']
    ids = ['Record/'+key('knowledge',package_id+':'+name) for name in ('P-14','P-22')]
    composed = pin(tmp_path,parent,child,child_content).data['candidate']
    lessons = [{'id':'joint','kind':'lesson','text':'Joint grounds','support_sets':[ids],'provenance':provenance},
               {'id':'alternative','kind':'lesson','text':'Independent alternative','support_sets':[[rid] for rid in ids],'provenance':provenance}]
    content = accept(parent,propose(parent,composed,lessons))
    changed = accept(child,propose(child,child_content,[dict(entries[0],state='withdrawn',reason='Withdrawn trial')]))
    new_pin = pin(tmp_path,parent,child,changed,base=content).data['candidate']
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
    candidate = pin(tmp_path,parent,child,first).data['candidate']
    publication = decide_candidate(context_for(tmp_path,parent,candidate,'accept')).publication
    package_id = child.read(first)['manifest']['package_id']
    moved = tmp_path/'moved'; child.base.rename(moved)
    (parent.base/'package-locations.json').write_text(json.dumps({package_id:str(moved/'atlas')}))
    request = ActionRequest('packages.inspect',publication=publication)
    result = inspect_packages(ActionContext(ServiceContext(tmp_path/'service'),request,parent.base,tmp_path/'evidence',{}))
    selected = next(node for node in result.data['packages'] if node['package_id']==package_id)
    assert selected['commit'] == first
    assert selected['mounts'] == ['shared/child']
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
    result = pin(tmp_path,parent,child,child_oid)
    composed = parent.read(result.data['candidate'])
    assert len(records_for(composed,result.data['candidate'])) == len(records_for(child.read(child_oid),child_oid))
    assert Publications(parent).ready() is None
