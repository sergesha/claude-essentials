"""Reviewed whole-layout composition using real Git objects and the service operation registry."""
from dataclasses import replace
import json
import uuid

import pytest

from knowledge_atlas.actions.decisions import inspect_candidate
from knowledge_atlas.api_models import parse_request
from knowledge_atlas.contracts import ActionContext, ActionRequest, AtlasError, ServiceContext
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.graph_stage import records_for
from knowledge_atlas.core.publication import Publications, commit_files
from knowledge_atlas.operations import inspect_operation, submit
from knowledge_atlas.project_registry import register_project
from knowledge_atlas.worker import run_next
from test_decisions import projection
from test_packages import publish, snapshot


def identity(store):
    return json.loads(git(store.root, 'show', 'HEAD:package.json'))['package_id']


def project(service, name, *, materials=False, remote=None):
    from knowledge_atlas.actions.atlas import create_atlas
    store = GitStore(service.service_root.parent/name)
    create_atlas(ActionContext(service, ActionRequest('atlas.create'), store.base, service.service_root, {}))
    if remote:
        git(store.root, 'remote', 'add', 'origin', remote)
        register_project(service, identity(store), store.base)
    if materials:
        snapshot(store)
    return store


def include(store, mount, publication):
    return {'package_id': identity(store), 'mount': mount, 'publication': publication}


def request(store, includes, expected=None):
    return parse_request('packages.configure', {
        'atlas_id': identity(store), 'request_id': str(uuid.uuid4()), 'expected_publication': expected,
        'payload': {'includes': includes}})


def configure(service, parent, includes, *, expected=None):
    operation = submit(service, request(parent, includes, expected))
    run_next(service)
    return inspect_operation(service, operation.operation_id)


def candidate(operation):
    assert operation.state == 'succeeded', operation.error
    return operation.result.data['candidate']


def proposals(store):
    return git(store.root, 'for-each-ref', '--format=%(objectname)', 'refs/poc9/proposals/').splitlines()


@pytest.fixture
def service(tmp_path):
    return ServiceContext(tmp_path/'service')


def test_replace_move_remove_and_clear_preserve_own_materials(service):
    parent = project(service, 'parent', materials=True)
    left, right = [project(service, name) for name in ('left', 'right')]
    left_pub, right_pub = [publish(store, git(store.root, 'rev-parse', 'HEAD')) for store in (left, right)]
    original = parent.read(git(parent.root, 'rev-parse', 'HEAD'))
    first = configure(service, parent, [include(left, 'topics/left', left_pub), include(right, 'right', right_pub)])
    first_oid = candidate(first)
    assert proposals(parent) == [first_oid] and Publications(parent).ready() is None
    ready = publish(parent, first_oid)
    result = configure(service, parent, [include(right, 'topics/moved', right_pub)], expected=ready)
    oid = candidate(result)
    inspection = inspect_candidate(ActionContext.for_read(service, ActionRequest('candidate.inspect',
        atlas_id=identity(parent), payload={'candidate': oid})))
    changes = inspection.data['composition_diff']
    assert changes == result.result.data['composition_diff']
    assert {x['mount'] for x in changes['removed']} == {'topics/left', 'right'}
    assert [(x['mount'], x['publication']) for x in changes['added']] == [('topics/moved', right_pub)]
    assert not git(parent.root, 'ls-tree', oid, '--', 'right', 'topics/left')
    config = git(parent.root, 'show', oid+':.gitmodules')
    assert 'topics/moved' in config and 'topics/left' not in config and 'path = right' not in config
    assert Publications(parent).ready() == ready
    before = parent.read(first_oid)
    ready2 = publish(parent, oid)
    empty = candidate(configure(service, parent, [], expected=ready2))
    assert parent.read(empty)['manifest']['modules'] == original['manifest']['modules']
    assert parent.read(empty)['sources'] == original['sources']
    assert parent.read(empty)['wiki'] == original['wiki']
    assert parent.read(empty)['relations'] == original['relations']
    assert not git(parent.root, 'ls-tree', empty, '--', 'topics')
    assert parent.read(first_oid) == before
    assert [Publications(s).ready() for s in (left,right)] == [left_pub,right_pub]


@pytest.mark.parametrize('mounts', [
    ['../child'], ['/tmp/child'], ['safe/.Git/child'], ['manifest.json'], ['review.json/child'],
    ['packages/sources/nested'], ['packages'], ['dup', 'dup'], ['area', 'area/child'], ['bad\\mount'],
])
def test_invalid_layout_is_atomic(service, mounts):
    parent = project(service, 'parent', materials=True)
    child = project(service, 'child')
    pub = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    ready = publish(parent, git(parent.root, 'rev-parse', 'HEAD'))
    original = parent.read(ready)
    op = configure(service, parent, [include(child, mount, pub) for mount in ['valid', *mounts]], expected=ready)
    assert op.state == 'failed' and op.error['code'] == 'invalid_package'
    assert proposals(parent) == [] and Publications(parent).ready() == ready
    assert parent.read(ready) == original


@pytest.mark.parametrize('problem', ['unknown', 'unavailable', 'wrong-wrapper', 'content', 'cycle', 'versions'])
def test_child_selection_conflicts_fail_before_exposure(service, problem):
    parent, child, other = [project(service, name) for name in ('parent','child','other')]
    pub = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    other_pub = publish(other, git(other.root, 'rev-parse', 'HEAD'))
    selections = [include(child, 'valid', pub)]
    selection = include(other, 'other', other_pub)
    if problem == 'unknown':
        selection['package_id'] = str(uuid.uuid4())
    elif problem == 'unavailable':
        other.base.rename(other.base.with_name('unavailable'))
    elif problem == 'wrong-wrapper':
        selection['publication'] = pub
    elif problem == 'content':
        selection['publication'] = Publications(other).read(other_pub)['content_commit']
    elif problem == 'cycle':
        parent_pub = publish(parent, git(parent.root, 'rev-parse', 'HEAD'))
        selection = include(parent, 'self', parent_pub)
    elif problem == 'versions':
        next_oid = candidate(configure(service, child, [], expected=pub))
        selection = include(child, 'other', publish(child, next_oid))
    ready = Publications(parent).ready()
    op = configure(service, parent, [*selections, selection], expected=ready)
    assert op.state == 'failed' and op.error['code'] in ('invalid_package','atlas_conflict','atlas_not_found')
    assert proposals(parent) == [] and Publications(parent).ready() == ready


def test_parent_precondition_and_exact_request_retry(service):
    parent, child = [project(service, name) for name in ('parent','child')]
    old = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    req = request(parent, [include(child, 'first', old)])
    op = submit(service, req)
    run_next(service)
    oid = candidate(inspect_operation(service, op.operation_id))
    publish(child, candidate(configure(service, child, [], expected=old)))
    retry = submit(service, req)
    assert retry.operation_id == op.operation_id and candidate(retry) == oid
    with pytest.raises(AtlasError, match='request_conflict'):
        submit(service, replace(req, payload={'includes': []}))
    ready = publish(parent, oid)
    stale = configure(service, parent, [])
    assert stale.state == 'failed' and stale.error['code'] == 'stale_publication'
    assert proposals(parent) == [oid] and Publications(parent).ready() == ready


def test_configure_requires_explicit_precondition_and_strict_includes(service):
    parent = project(service, 'parent')
    body = {'atlas_id': identity(parent), 'request_id': str(uuid.uuid4()), 'payload': {'includes': []}}
    with pytest.raises(AtlasError, match='expected_publication'):
        parse_request('packages.configure', body)
    body.update(expected_publication=None, payload={'includes': [{'package_id': identity(parent),
        'mount': 'a', 'publication': '0'*40, 'commit': '1'*40}]})
    with pytest.raises(AtlasError, match='commit'):
        parse_request('packages.configure', body)


def test_configure_retains_transitive_receipts_before_exposure(service):
    parent, branch = [project(service, name) for name in ('parent', 'branch')]
    shared = project(service, 'shared', materials=True)
    wrapper = publish(shared, git(shared.root, 'rev-parse', 'HEAD'))
    receipt = Publications(shared).read(wrapper)
    content = receipt['content_commit']
    alternate = commit_files(shared.root, content, {'publication.json': {**receipt, 'provider_run': 'second'}},
                             [content], 'Alternate projection receipt for the same reviewed content')
    left = candidate(configure(service, branch, [include(shared, 'nested', wrapper)]))
    branch_pub = publish(branch, left)
    # Force the parent request to establish transitive retention itself.
    for ref in git(shared.root, 'for-each-ref', '--format=%(refname)').splitlines():
        if ref != Publications.ref and git(shared.root, 'rev-parse', ref) == wrapper:
            git(shared.root, 'update-ref', '-d', ref)
    oid = candidate(configure(service, parent, [include(branch, 'field', branch_pub),
                                             include(shared, 'second', alternate)]))
    for store in (branch, shared):
        git(store.root, 'update-ref', '-d', Publications.ref)
        git(store.root, 'reflog', 'expire', '--expire=now', '--all')
        git(store.root, 'gc', '--prune=now')
    for retained, store in [(branch_pub,branch),(wrapper,shared),(alternate,shared)]:
        assert Publications(store).read(retained)['content_commit']
    nodes = parent.closure(oid)
    shared_node, = [node for node in nodes if node['package_id'] == identity(shared)]
    assert {row['mount']: row['publication'] for row in shared_node['inclusions']} == {
        'field/nested': wrapper, 'second': alternate}
    records = [json.loads(r['payload']) for r in records_for(parent.read(oid), oid)]
    own = [r for r in records if r.get('package_id') == identity(shared)]
    assert len(own) == len(records_for(shared.read(content), content))
    assert all(r['mounts'] == ['field/nested','second'] for r in own)
    assert all(r['inclusions'] == [{'mount': 'field/nested', 'publication': wrapper},
                                   {'mount': 'second', 'publication': alternate}] for r in own)
    assert Publications(parent).ready() is None


@pytest.mark.parametrize('corrupt', ['url', 'extra-gitlink', 'extra-section', 'pin', 'receipt', 'no-receipt', 'no-kind', 'unknown-kind'])
def test_read_rejects_inconsistent_layout(service, corrupt):
    parent, child = [project(service, name) for name in ('parent','child')]
    receipt = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    oid = candidate(configure(service, parent, [include(child, 'child', receipt)]))
    manifest = parent.read(oid)['manifest']
    mod, = manifest['modules'].values()
    config = git(parent.root, 'show', oid+':.gitmodules', binary=True)
    files, links = {}, {}
    if corrupt == 'url':
        files['.gitmodules'] = config.replace(mod['url'].encode(), b'../other')
    elif corrupt == 'extra-gitlink':
        links['extra'] = mod['commit']
    elif corrupt == 'extra-section':
        files['.gitmodules'] = config + b'[submodule "extra"]\npath = extra\nurl = ../extra\n'
    elif corrupt == 'pin':
        links['child'] = receipt
    else:
        if corrupt == 'receipt':
            mod['publication'] = mod['commit']
        else:
            mod.pop('publication')
            mod.pop('kind')
            if corrupt != 'no-kind':
                mod['kind'] = 'atlas' if corrupt == 'no-receipt' else 'leaf'
        files['manifest.json'] = manifest
    broken = commit_files(parent.root, oid, files, [oid], 'Incoherent layout', gitlinks=links)
    with pytest.raises(ValueError):
        parent.read(broken)


def test_registered_remote_locator_uses_service_store_for_review(service, monkeypatch):
    parent = project(service, 'parent')
    url = 'https://git.example.invalid/children/child.git'
    child = project(service, 'child', remote=url)
    receipt = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    op = configure(service, parent, [include(child, 'domain', receipt)])
    oid = candidate(op)
    result = inspect_candidate(ActionContext.for_read(service, ActionRequest('candidate.inspect',
        atlas_id=identity(parent), payload={'candidate': oid})))
    assert result.data['snapshot']['included_packages'][0]['package_id'] == identity(child)
    assert url in git(parent.root, 'show', oid+':.gitmodules')
    assert not (parent.root/'domain').exists()
    from knowledge_atlas.actions import decisions
    from knowledge_atlas.actions.atlas import atlas_status
    from test_decisions import context_for
    monkeypatch.setattr(decisions, 'build_projection', lambda context, content: projection(content))
    decision = context_for(service.service_root.parent, parent, oid, 'accept')
    published = decisions.decide_candidate(replace(decision, service=service)).publication
    status = atlas_status(ActionContext.for_read(service, ActionRequest('atlas.status', atlas_id=identity(parent))))
    assert status.publication == published and status.data['candidate_statuses'][oid] == 'accepted'


@pytest.mark.parametrize('interruption', ['retention', 'parent-advanced'])
def test_failed_retention_or_concurrent_parent_publication_never_exposes_layout(service, monkeypatch, interruption):
    from knowledge_atlas.core import composition
    parent, child = [project(service, name) for name in ('parent','child')]
    receipt = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    retain = composition.retain_child_publication

    def interrupt(*args):
        if interruption == 'retention':
            raise ValueError('retention unavailable')
        retain(*args)
        publish(parent, git(parent.root, 'rev-parse', 'HEAD'))

    monkeypatch.setattr(composition, 'retain_child_publication', interrupt)
    op = configure(service, parent, [include(child, 'domain', receipt)])
    assert op.state == 'failed'
    assert op.error['code'] == ('invalid_package' if interruption == 'retention' else 'stale_publication')
    assert proposals(parent) == []
    assert bool(Publications(parent).ready()) == (interruption == 'parent-advanced')

def test_imported_child_wrapper_must_share_reviewed_tree(service):
    parent, child = [project(service, name) for name in ('parent', 'child')]
    receipt = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    oid = candidate(configure(service, parent, [include(child, 'child', receipt)]))
    manifest = parent.read(oid)['manifest']
    mod, = manifest['modules'].values()
    forged = commit_files(child.root, receipt, {'extra.txt': b'Unreviewed content'}, [mod['commit']],
                          'Wrapper with a different tree')
    mod['publication'] = forged
    damaged = commit_files(parent.root, oid, {'manifest.json': manifest}, [oid], 'Unverified child wrapper')
    with pytest.raises(ValueError):
        parent.read(damaged)


def test_retention_rejects_foreign_project_objects_in_child_repository(service):
    from knowledge_atlas.core.composition import retain_child_publication
    owner, foreign = [project(service, n) for n in ('owner', 'foreign')]
    receipt = publish(foreign, git(foreign.root, 'rev-parse', 'HEAD'))
    content = Publications(foreign).read(receipt)['content_commit']
    git(owner.root, 'fetch', str(foreign.root), receipt)
    with pytest.raises(ValueError):
        retain_child_publication(owner, receipt, content)
    assert not git(owner.root, 'for-each-ref', 'refs/poc9/retained/')

def test_registered_child_keeps_archive_local_remapping_inside_new_parent(service):
    parent, branch, nested = [project(service, n) for n in ('parent', 'branch', 'nested')]
    wrapped = publish(nested, git(nested.root, 'rev-parse', 'HEAD'))
    selected = publish(branch, candidate(configure(service, branch, [include(nested, 'nested', wrapped)])))
    nested_id = identity(nested)
    moved = branch.base/'modules/0'
    moved.parent.mkdir()
    nested.root.rename(moved)
    (branch.base/'package-locations.json').write_text(json.dumps({nested_id: 'modules/0'}))
    assert Publications(branch).read(selected)
    oid = candidate(configure(service, parent, [include(branch, 'branch', selected)]))
    read = inspect_candidate(ActionContext.for_read(service, ActionRequest('candidate.inspect',
        atlas_id=identity(parent), payload={'candidate': oid})))
    assert read.data['snapshot']['included_packages'][0]['snapshot']['included_packages'][0]['package_id'] == nested_id

def test_relative_registered_origin_allows_recursive_git_checkout(service):
    parent = project(service, 'parent')
    child = project(service, 'deep/child', remote='../../../published.git')
    wrapper = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    content = Publications(child).read(wrapper)['content_commit']
    directory = service.service_root.parent
    git(directory, 'init', '--bare', 'published.git')
    git(directory/'published.git', 'fetch', str(child.root), content+':refs/heads/data')
    oid = candidate(configure(service, parent, [include(child, 'field', wrapper)]))
    git(directory, 'clone', '--no-checkout', str(parent.root), 'clone')
    clone = directory/'clone'
    git(clone, 'fetch', str(parent.root), oid)
    git(clone, 'checkout', '--detach', oid)
    git(clone, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive')
    assert git(clone/'field', 'rev-parse', 'HEAD') == content


def test_nested_conflicting_versions_and_reviewed_rollback(service):
    parent, branch, child = [project(service, n) for n in ('parent', 'branch', 'child')]
    old = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    branch_pub = publish(branch, candidate(configure(service, branch, [include(child, 'leaf', old)])))
    ready = publish(parent, candidate(configure(service, parent, [include(child, 'topic', old)])))
    newer = publish(child, candidate(configure(service, child, [], expected=old)))
    proposals_before = proposals(parent)
    conflict = configure(service, parent, [include(branch, 'branch', branch_pub), include(child, 'topic', newer)],
                         expected=ready)
    assert conflict.state == 'failed' and 'conflicting pins' in conflict.error['message']
    assert proposals(parent) == proposals_before and Publications(parent).ready() == ready
    repin = configure(service, parent, [include(child, 'topic', newer)], expected=ready)
    updated = candidate(repin)
    change, = repin.result.data['composition_diff']['changed']
    assert (change['before']['publication'], change['after']['publication']) == (old, newer)
    assert Publications(parent).ready() == ready
    second = publish(parent, updated)
    rollback = candidate(configure(service, parent, [include(child, 'topic', old)], expected=second))
    assert Publications(parent).ready() == second
    last = publish(parent, rollback)
    for publication, expected in [(ready, old), (second, newer), (last, old)]:
        mod, = parent.read(publication)['manifest']['modules'].values()
        assert mod['publication'] == expected

def recovery_copy(parent, child):
    """Reproduce the archive layout: flat independent repos and a UUID location map."""
    import shutil

    locations = {}
    for node in child.closure(git(child.root, 'rev-parse', 'HEAD')):
        path = parent.base / 'modules' / node['package_id']
        shutil.copytree(node['path'], path)
        locations[node['package_id']] = str(path.relative_to(parent.base))
    (parent.base / 'package-locations.json').write_text(json.dumps(locations))
    return parent.at_repository(parent.base / locations[identity(child)])


def gc(repo):
    git(repo, 'reflog', 'expire', '--expire=now', '--all')
    git(repo, 'gc', '--prune=now')


def test_distinct_recovery_stores_retain_each_exact_diamond_receipt(service):
    from knowledge_atlas.project_registry import project_store

    parent, left, right = [project(service, name) for name in ('parent', 'left', 'right')]
    leaf = project(service, 'leaf', materials=True)
    first = publish(leaf, git(leaf.root, 'rev-parse', 'HEAD'))
    receipt = Publications(leaf).read(first)
    content = receipt['content_commit']
    second = commit_files(leaf.root, content, {'publication.json': {**receipt, 'provider_run': 'alternate'}},
                          [content], 'Another receipt for the same content')
    branches = []
    copies = []
    for branch, selected in ((left, first), (right, second)):
        branch_pub = publish(branch, candidate(configure(service, branch, [include(leaf, 'leaf', selected)])))
        branches.append(include(branch, branch.base.name, branch_pub))
        copy = recovery_copy(branch, leaf)
        for ref in git(copy.root, 'for-each-ref', '--format=%(refname)').splitlines():
            if git(copy.root, 'rev-parse', ref) in (first, second):
                git(copy.root, 'update-ref', '-d', ref)
        git(copy.root, 'update-ref', 'refs/fixture/selected', selected)
        gc(copy.root)
        assert Publications(project_store(service, branch.base)).read(branch_pub)
        with pytest.raises(ValueError):
            git(copy.root, 'cat-file', '-e', second if selected == first else first)
        copies.append((copy, selected))

    parent_store = project_store(service, parent.base)
    oid = candidate(configure(service, parent, branches))
    for copy, selected in copies:
        git(copy.root, 'update-ref', '-d', 'refs/fixture/selected')
        gc(copy.root)
        assert Publications(copy).read(selected)['content_commit'] == content

    ready = publish(parent_store, oid)
    records = [json.loads(item['payload']) for item in records_for(parent_store.read(ready), oid)]
    leaf_records = [item for item in records if item.get('package_id') == identity(leaf)]
    assert len(leaf_records) == len(records_for(leaf.read(content), content))
    assert all(item['inclusions'] == [{'mount': 'left/leaf', 'publication': first},
                                      {'mount': 'right/leaf', 'publication': second}] for item in leaf_records)


def test_restored_parent_repins_registered_child_and_keeps_older_copy(service):
    from knowledge_atlas.project_registry import project_store

    parent = project(service, 'parent')
    child = project(service, 'child', materials=True)
    first = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    ready = publish(parent, candidate(configure(service, parent, [include(child, 'child', first)])))
    old_copy = recovery_copy(parent, child)
    # A different receipt may select the same content already present in the copy.
    receipt = Publications(child).read(first)
    content = receipt['content_commit']
    alternate = commit_files(child.root, content, {'publication.json': {**receipt, 'provider_run': 'alternate'}},
                             [content], 'Another publication of this content')
    same_content = candidate(configure(service, parent, [include(child, 'child', alternate)], expected=ready))
    revised = snapshot(child, 2)
    manifest = child.read(revised)['manifest']
    manifest['proposal_base_publication'] = first
    revised = commit_files(child.root, revised, {'manifest.json': manifest}, [revised], 'Review updated evidence')
    second = publish(child, revised)
    with pytest.raises(ValueError):
        git(old_copy.root, 'cat-file', '-e', second)

    parent_store = project_store(service, parent.base)
    proposed = candidate(configure(service, parent, [include(child, 'child', second)], expected=ready))
    assert Publications(parent_store).ready() == ready
    assert parent_store.read(proposed)['included_packages'][0]['snapshot']['evidence'] == {'version': 2}
    newer = publish(parent_store, proposed)
    git(child.root, 'update-ref', '-d', Publications.ref)
    gc(child.root)
    gc(old_copy.root)
    same, = project_store(service, parent.base).read(same_content)['included_packages']
    assert same['commit'] == content and same['inclusions'] == [{'mount': 'child', 'publication': alternate}]
    for pub, child_pub, version in [(ready, first, 0), (newer, second, 2)]:
        store = project_store(service, parent.base)
        selected = Publications(store).read(pub)['content_commit']
        inclusion, = store.read(selected)['included_packages']
        assert inclusion['inclusions'] == [{'mount': 'child', 'publication': child_pub}]
        assert inclusion['snapshot']['evidence'] == {'version': version}
        historical = store.read_package(identity(child), inclusion['commit'], atlas_commit=selected)
        assert historical['evidence'] == {'version': version}
    child.base.rename(child.base.with_name('unavailable-child'))
    assert Publications(project_store(service, parent.base)).read(ready)


@pytest.mark.parametrize('name', ['sources', 'knowledge'])
def test_own_leaf_role_must_match_retained_content(service, name):
    parent = project(service, 'parent', materials=True)
    head = git(parent.root, 'rev-parse', 'HEAD')
    manifest = parent.read(head)['manifest']
    original = manifest['modules'][name]
    child = manifest['modules']['knowledge' if name == 'sources' else 'sources']
    original.update({key:child[key] for key in ('package_id','url','commit')})
    config = git(parent.root, 'show', head+':.gitmodules', binary=True)
    old_url = ('../modules/' + name).encode()
    config = config.replace(old_url, child['url'].encode())
    broken = commit_files(parent.root, head, {'manifest.json':manifest, '.gitmodules':config}, [head],
                          'Swap leaf content', gitlinks={original['mount']:child['commit']})
    with pytest.raises(ValueError, match='content module|package roles'):
        parent.closure(broken)


def test_projection_requires_each_child_mount_receipt(service):
    parent, child = [project(service, name) for name in ('parent','child')]
    pub = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    oid = candidate(configure(service, parent, [include(child, 'child', pub)]))
    retained = parent.read(oid)
    retained['included_packages'][0]['inclusions'][0]['publication'] = None
    with pytest.raises(ValueError, match='publication'):
        records_for(retained, oid)
