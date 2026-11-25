"""Exact per-inclusion Git receipts through GC, recursive checkout and portable recovery."""
import pytest

from knowledge_atlas.core import composition
from knowledge_atlas.core.archive import export_atlas, restore_atlas
from knowledge_atlas.core.git_store import git
from knowledge_atlas.core.publication import Publications, commit_files
from knowledge_atlas.project_registry import project_store
from test_archives import portable
from test_composition_config import (
    candidate, configure, gc, identity, include, project, recovery_copy, service,
)


def publish(store, candidate):
    from test_decisions import projection
    from test_packages import publish as publish_candidate
    from knowledge_atlas.core.graph_stage import records_for

    def empty_projection(oid):
        assert records_for(store.read(oid), oid) == []
        return {**projection(oid), 'record_count':0, 'chunk_count':0, 'vector_artifacts':{}}

    return publish_candidate(store, candidate, empty_projection)


def publish_parent(service, parent, layout, portable, expected=None):
    oid = candidate(configure(service, parent, layout, expected=expected))
    pubs = Publications(project_store(service, parent.base))
    content = pubs.review(oid, {'decision': 'accept', 'kind': 'synthetic', 'actor': 'Fixture',
        'reason': 'Review archived layout', 'accepted_at': '2026-11-13T02:00:00Z',
        'base_publication': expected}, portable[4])
    return pubs.publish(content, lambda oid:portable[5](oid, pubs.store))


def selected(child, publication, mount):
    return {**include(child, mount, publication), 'commit': Publications(child).read(publication)['content_commit']}


def test_pending_and_historical_receipts_survive_child_gc(service, portable):
    parent, child = [project(service, n) for n in ('parent', 'child')]
    old = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    first = publish_parent(service, parent, [include(child, 'old', old)], portable)
    cleared = publish_parent(service, parent, [], portable, first)
    new = publish(child, candidate(configure(service, child, [], expected=old)))
    pending = candidate(configure(service, parent, [include(child, 'new', new)], expected=cleared))
    git(child.root, 'update-ref', Publications.ref, old, new)
    gc(child.root)
    for state, wrapper, mount in [(first, old, 'old'), (pending, new, 'new')]:
        assert composition.composition_receipts(parent, state) == [selected(child, wrapper, mount)]
    assert composition.composition_receipts(parent, cleared) == []


def test_nested_receipt_export_without_current_child_ready(service, portable, tmp_path):
    parent, left, right, leaf = [project(service, n) for n in ('parent', 'left', 'right', 'leaf')]
    first = publish(leaf, git(leaf.root, 'rev-parse', 'HEAD'))
    receipt = Publications(leaf).read(first)
    content = receipt['content_commit']
    second = commit_files(leaf.root, content, {'publication.json': {**receipt, 'provider_run': 'alternate'}},
                          [content], 'Another valid publication of the same content')
    inclusions = []
    copies = []
    decisions = []
    from test_decisions import context_for
    from knowledge_atlas.actions.decisions import decide_candidate, decision_inventory
    from knowledge_atlas.core.decision_receipts import DECISIONS
    for branch, wrapper in [(left, first), (right, second)]:
        branch_pub = publish(branch, candidate(configure(service, branch, [include(leaf, 'leaf', wrapper)])))
        pending = candidate(configure(service, leaf, [], expected=first))
        rejected = decide_candidate(context_for(tmp_path, leaf, pending, 'reject', first))
        decision_ref = DECISIONS + rejected.data['decision']['decision_id']
        decisions.append(decision_ref)
        copy = recovery_copy(branch, leaf)
        git(leaf.root, 'update-ref', '-d', decision_ref)
        git(copy.root, 'update-ref', 'refs/fixture/selected', wrapper)
        for ref in git(copy.root, 'for-each-ref', '--format=%(refname)').splitlines():
            if ref != 'refs/fixture/selected' and git(copy.root, 'rev-parse', ref) in (first, second):
                git(copy.root, 'update-ref', '-d', ref)
        gc(copy.root)
        copies.append((copy, wrapper))
        inclusions.append(include(branch, branch.base.name, branch_pub))
    ready = publish_parent(service, parent, inclusions, portable)
    # The wrappers exist only as unreferenced objects in distinct local copies.
    # Each parent edge must explicitly carry its receipt into the bundle.
    for copy, wrapper in copies:
        git(copy.root, 'update-ref', '-d', 'refs/fixture/selected')
        git(copy.root, 'update-ref', '-d', 'refs/poc9/retained/publications/' + wrapper)
    leaf.base.rename(tmp_path / 'original-leaf-unavailable')
    store = project_store(service, parent.base)
    archive = tmp_path / 'portable.zip'
    export_atlas(store, archive, tokenizer=portable[1], formatter=portable[2])
    restored = restore_atlas(archive, tmp_path / 'recovered')
    assert Publications(restored).ready() == ready
    shared, = [node for node in restored.closure(ready) if 'left/leaf' in node['mounts']]
    assert {r['mount']: r['publication'] for r in shared['inclusions']} == {'left/leaf': first, 'right/leaf': second}
    saved_decisions = decision_inventory(restored.at_repository(shared['path']))
    assert {entry['ref'] for entry in saved_decisions} == set(decisions)
    for wrapper in (first, second):
        assert git(shared['path'], 'rev-parse', 'refs/poc9/retained/publications/' + wrapper) == wrapper


def test_restore_after_original_paths_disappear(service, portable, tmp_path):
    parent, child = [project(service, n) for n in ('parent', 'child')]
    old = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    first = publish_parent(service, parent, [include(child, 'materials', old)], portable)
    new = publish(child, candidate(configure(service, child, [], expected=old)))
    last = publish_parent(service, parent, [include(child, 'materials', new)], portable, first)
    expected = {pub: selected(child, wrapper, 'materials') for pub, wrapper in [(first, old), (last, new)]}
    archive = tmp_path / 'portable.zip'
    export_atlas(parent, archive, tokenizer=portable[1], formatter=portable[2])
    for store in (parent, child):
        store.base.rename(store.base.with_name(store.base.name + '-unavailable'))
    restored = restore_atlas(archive, tmp_path / 'restored')
    for pub, binding in expected.items():
        assert composition.composition_receipts(restored, pub) == [binding]
        assert all(node['path'].is_relative_to(restored.base) for node in restored.closure(pub))


def test_recursive_clone_with_real_locators(service, tmp_path):
    parent, child, leaf = [project(service, n) for n in ('parent', 'child', 'leaf')]
    leaf_pub = publish(leaf, git(leaf.root, 'rev-parse', 'HEAD'))
    child_pub = publish(child, candidate(configure(service, child, [include(leaf, 'nested', leaf_pub)])))
    parent_pub = publish(parent, candidate(configure(service, parent, [include(child, 'field', child_pub)])))
    git(tmp_path, '-c', 'protocol.file.allow=always', 'clone', '--recursive', '-b', 'ready', str(parent.root), 'clone')
    clone = tmp_path / 'clone'
    assert git(clone, 'rev-parse', 'HEAD') == parent_pub
    for path, owner, publication in [('field', child, child_pub), ('field/nested', leaf, leaf_pub)]:
        assert git(clone/path, 'rev-parse', 'HEAD') == Publications(owner).read(publication)['content_commit']


def test_registry_reads_without_submodule_worktrees(service):
    parent, child = [project(service, n, remote='https://example.invalid/' + n) for n in ('parent', 'child')]
    child_pub = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    oid = candidate(configure(service, parent, [include(child, 'materials', child_pub)]))
    assert not (parent.root/'materials').exists()
    assert composition.composition_receipts(project_store(service, parent.base), oid) == [
        selected(child, child_pub, 'materials')]


@pytest.mark.parametrize('damage', ['wrong-wrapper', 'receipt-free-pending', 'receipt-free-ancestor'])
def test_restore_rejects_wrong_wrapper_in_child_only_history(service, portable, tmp_path, damage):
    import hashlib
    import json
    import zipfile
    import pytest

    parent, child, leaf = [project(service, n) for n in ('parent', 'child', 'leaf')]
    first = publish(leaf, git(leaf.root, 'rev-parse', 'HEAD'))
    second = publish(leaf, candidate(configure(service, leaf, [], expected=first)))
    child_pub = publish(child, candidate(configure(service, child, [include(leaf, 'leaf', first)])))
    publish_parent(service, parent, [include(child, 'child', child_pub)], portable)
    source = tmp_path / 'original.zip'
    inventory = export_atlas(parent, source, tokenizer=portable[1], formatter=portable[2])
    child_content = Publications(child).read(child_pub)['content_commit']
    manifest = child.read(child_content)['manifest']
    mod, = manifest['modules'].values()
    if damage == 'wrong-wrapper':
        mod['publication'] = second
    else:
        del mod['publication']
    bad = commit_files(child.root, child_content, {'manifest.json': manifest}, [child_content], 'Invalid historical child selection')
    if damage == 'receipt-free-ancestor':
        bad = commit_files(child.root, child_content, {}, [bad], 'Valid layout retaining unsupported ancestor')
    git(child.root, 'update-ref', 'refs/poc9/proposals/' + bad, bad)
    bundle = tmp_path / 'modified.bundle'
    git(child.root, 'bundle', 'create', str(bundle), '--all')
    name = next(row['bundle'] for row in inventory['packages'] if row['package_id'] == identity(child))
    contents = bundle.read_bytes()
    inventory['sha256'][name] = hashlib.sha256(contents).hexdigest()
    broken = tmp_path / 'modified.zip'
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(broken, 'w') as after:
        for member in before.namelist():
            if member == 'archive.json':
                after.writestr(member, json.dumps(inventory))
            elif member == name:
                after.writestr(member, contents)
            else:
                after.writestr(member, before.read(member))
    before_restore = broken.read_bytes()
    with pytest.raises(ValueError, match='publication'):
        restore_atlas(broken, tmp_path / 'recovered')
    assert not (tmp_path / 'recovered').exists()
    assert broken.read_bytes() == before_restore


def test_archive_export_reports_missing_required_wrapper(service, portable, tmp_path):
    import shutil
    import pytest
    from knowledge_atlas.actions.archives import export_archive
    from knowledge_atlas.contracts import ActionContext, ActionRequest, AtlasError

    parent, child = [project(service, n) for n in ('parent', 'child')]
    wrapper = publish(child, git(child.root, 'rev-parse', 'HEAD'))
    ready = publish_parent(service, parent, [include(child, 'child', wrapper)], portable)
    for ref in git(child.root, 'for-each-ref', '--format=%(refname)').splitlines():
        if git(child.root, 'rev-parse', ref) == wrapper:
            git(child.root, 'update-ref', '-d', ref)
    gc(child.root)
    shutil.copytree(portable[1].parent, parent.base/'assets')
    path = tmp_path/'broken.zip'
    context = ActionContext(service, ActionRequest('archive.export', atlas_id=identity(parent),
        expected_publication=ready, payload={'path': str(path)}), parent.base, tmp_path, {})
    with pytest.raises(AtlasError) as exc:
        export_archive(context)
    assert exc.value.code == 'invalid_archive'
    assert not path.exists()
