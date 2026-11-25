"""Versioned child layouts and retention in the owning Git repositories."""
from datetime import datetime
import hashlib
import os
import re
from pathlib import Path, PureWindowsPath
import tempfile

from .git_store import GitStore, git, package_identity
from .publication import Publications, StaleProposal, commit_files


# These files can be written by later review, publication and knowledge operations.
_RESERVED = {'.gitmodules', 'manifest.json', 'package.json', 'publication.json', 'review.json',
             'relations.json', 'knowledge-records.json', 'embedding-profile.json', 'proposal.json', 'atlas-decision.json'}


def _validate_mounts(mounts, files=()):
    occupied = {path.casefold() for path in (*_RESERVED, *files)}
    for mount in mounts:
        if (not isinstance(mount, str) or not mount or PureWindowsPath(mount).drive
                or '\\' in mount or any(ord(char) < 32 or ord(char) == 127 for char in mount)
                or any(part.casefold() in ('', '.', '..', '.git') for part in mount.split('/'))):
            raise ValueError('safe relative package mount required')
        path = mount.casefold()
        if any(path == used or path.startswith(used + '/') or used.startswith(path + '/')
               for used in occupied):
            raise ValueError('reserved, duplicate or overlapping package mount: ' + mount)
        occupied.add(path)


def _tree_entries(repo, oid):
    entries = git(repo, 'ls-tree', '-rz', oid, binary=True).split(b'\0')
    tree = {}
    for entry in filter(None, entries):
        meta, name = entry.split(b'\t', 1)
        mode, _, target = meta.decode().split()
        tree[os.fsdecode(name)] = (mode, target)
    return tree


def validate_layout(repo, oid, modules):
    """Require the manifest, Git's submodule sections and all tree gitlinks to agree."""
    tree = _tree_entries(repo, oid)
    links = {path: target for path, (mode, target) in tree.items() if mode == '160000'}
    _validate_mounts([mod['mount'] for mod in modules.values()], tree.keys() - links.keys())
    if links != {mod['mount']: mod['commit'] for mod in modules.values()}:
        raise ValueError('manifest/gitlink mismatch')
    expected = {}
    for name, mod in modules.items():
        expected[(name, 'path')] = mod['mount']
        expected[(name, 'url')] = mod['url']
        if name in ('sources', 'knowledge'):
            if 'kind' in mod or 'publication' in mod:
                raise ValueError('invalid own content module')
        elif (mod.get('kind') != 'atlas' or mod.get('scope') != 'all'
              or not isinstance(mod.get('publication'), str)
              or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', mod['publication'])):
            raise ValueError('child atlas requires an exact publication and all scope')
    actual = {}
    if '.gitmodules' in tree:
        config = git(repo, 'config', '--blob', oid + ':.gitmodules', '--null', '--list', binary=True)
        for record in filter(None, config.split(b'\0')):
            key, _, value = record.decode().partition('\n')
            if not key.startswith('submodule.'):
                continue
            name, _, field = key.removeprefix('submodule.').rpartition('.')
            if name not in modules:
                raise ValueError('undeclared submodule section')
            if field in ('path', 'url'):
                if (name, field) in actual:
                    raise ValueError('duplicate submodule metadata')
                actual[(name, field)] = value
    if actual != expected:
        raise ValueError('manifest/.gitmodules mismatch')


def composition_diff(before, after):
    """A moved mount removes its old path and adds its new one."""
    def layout(modules):
        return {mod['mount']: {key: mod[key] for key in ('mount', 'package_id', 'commit', 'publication', 'scope', 'url')
                               if key in mod} for mod in modules if mod.get('kind') == 'atlas'}

    old, new = layout(before), layout(after)
    return {'removed': [old[path] for path in sorted(old.keys() - new.keys())],
            'added': [new[path] for path in sorted(new.keys() - old.keys())],
            'changed': [{'before': old[path], 'after': new[path]} for path in sorted(old.keys() & new.keys())
                        if old[path] != new[path]]}


def composition_receipts(store: GitStore, commit: str) -> list[dict]:
    """Return each verified child wrapper/content selection, including nested mounts."""
    receipts = [
        {'package_id': node['package_id'], 'commit': node['commit'], **inclusion}
        for _, node in store.repository_closure(commit)
        for inclusion in node.get('inclusions', ())
    ]
    return sorted(receipts, key=lambda row: row['mount'])


def retain_child_publication(child: GitStore, publication: str, content_commit: str) -> None:
    """Pin the exact verified wrapper. Its Git parents retain the reviewed content."""
    Publications(child).verify_child_receipt(publication, content_commit)
    owner = package_identity(child.root, 'HEAD')
    selected = package_identity(child.root, content_commit)
    if selected != owner:
        raise ValueError('child publication belongs to another UUID')
    git(child.root, 'update-ref', 'refs/poc9/retained/publications/' + publication, publication)


def configure_composition(store: GitStore, base: str, resolved: list[dict], *, recorded_at: str,
                          base_publication: str | None) -> str:
    pubs = Publications(store)
    if pubs.ready() != base_publication:
        raise StaleProposal('stale parent publication')
    origin = pubs.read(base_publication)['content_commit'] if base_publication else git(store.root, 'rev-parse', 'HEAD')
    if base != origin:
        raise StaleProposal('layout must derive from the expected parent publication')
    if datetime.fromisoformat(recorded_at.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('recorded_at requires timezone')
    manifest = store.read(base)['manifest']
    previous = manifest['modules']
    modules = {key: mod for key, mod in previous.items() if mod.get('kind') != 'atlas'}
    removed = {key: mod for key, mod in previous.items() if mod.get('kind') == 'atlas'}
    tree = _tree_entries(store.root, base)
    files = [path for path, (mode, _) in tree.items() if mode != '160000']
    _validate_mounts([mod['mount'] for mod in [*modules.values(), *resolved]], files)
    links = {}
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / 'gitmodules'
        config.write_bytes(git(store.root, 'show', base + ':.gitmodules', binary=True) if '.gitmodules' in tree else b'')
        for key in removed:
            git(store.root, 'config', '-f', str(config), '--remove-section', 'submodule.' + key)
        for item in resolved:
            mount = item['mount']
            key = 'included-' + hashlib.sha256(mount.encode()).hexdigest()[:24]
            if key in modules:
                raise ValueError('existing module section collision')
            mod = {field: item[field] for field in ('package_id', 'mount', 'publication', 'commit', 'url')}
            mod.update(kind='atlas', scope='all')
            modules[key] = mod
            links[mount] = mod['commit']
            git(store.root, 'config', '-f', str(config), 'submodule.' + key + '.path', mount)
            git(store.root, 'config', '-f', str(config), 'submodule.' + key + '.url', mod['url'])
        manifest = {**manifest, 'modules': modules, 'status': 'candidate', 'accepted_at': None,
                    'recorded_at': recorded_at, 'proposal_base_publication': base_publication,
                    'proposal_base_content': base}
        manifest.pop('acceptance', None)
        candidate = commit_files(store.root, base, {'manifest.json': manifest, '.gitmodules': config.read_bytes()},
                                 [base], 'Propose reviewed child layout', gitlinks=links,
                                 remove_paths=[mod['mount'] for mod in removed.values()])

    # Walk the complete proposed tree before making any ref visible. Gitlinks
    # cross object stores; the owner's refs must keep each such target alive.
    owners = store.repository_closure(candidate)
    for child, node in owners[1:]:
        for receipt in sorted({row['publication'] for row in node.get('inclusions', ())}):
            retain_child_publication(child, receipt, node['commit'])
        git(child.root, 'update-ref', 'refs/poc9/retained/contents/' + node['commit'], node['commit'])
    proposal = 'refs/poc9/proposals/' + candidate
    expected = base_publication or '0' * len(candidate)
    transaction = f'verify {pubs.ref} {expected}\nupdate {proposal} {candidate}\n'
    try:
        git(store.root, 'update-ref', '--stdin', data=transaction.encode())
    except ValueError as error:
        if pubs.ready() != base_publication:
            raise StaleProposal('stale parent publication') from error
        raise
    return candidate
