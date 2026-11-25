"""Portable Git package closure and retained document vectors.

Bundles preserve canonical commits; the restored provider cache is disposable.
Native graph/search projections must subsequently be rebuilt.
"""
from contextlib import closing, contextmanager
import hashlib
import io
import os
import stat
import json
from pathlib import Path
import re
import tempfile
import zipfile
import zlib
from .git_store import GitStore, git, _json, package_identity
from ..storage import FORMAT
from .publication import Publications
from .graph_stage import records_for
from .vector_chunks import chunks_for
from .decision_receipts import DECISIONS, decision_inventory
from .composition import retain_child_publication
from .embedding_provider import EmbeddingProvider, canonical, artifact_key, cache_artifact


# Limits include archive.json.
MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_MEMBER_BYTES = 256 * 1024**2
MAX_EXPANDED_BYTES = 1024**3
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_ARCHIVE_ENTRIES = 1024
MAX_DIRECTORY_BYTES = 512 * 1024
COPY_CHUNK_BYTES = 1024**2


def copy_bounded(source, destination, limit, digest=None):
    copied = 0
    while True:
        block = source.read(min(COPY_CHUNK_BYTES, limit - copied + 1))
        if not block:
            return copied
        copied += len(block)
        if copied > limit:
            raise ValueError('archive exceeds byte limit')
        destination.write(block)
        if digest is not None:
            digest.update(block)


@contextmanager
def regular_archive(path):
    # Traverse the resolved path on pinned directory descriptors. A replacement
    # symlink cannot redirect a checked path between validation and open.
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('archive must be a regular file')
        fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(info, opened) or not stat.S_ISREG(opened.st_mode):
            raise ValueError('archive changed during open')
        # A multiply-linked file can also be named inside a private data root.
        if opened.st_nlink != 1:
            raise ValueError('archive must have a single filesystem link')
        if opened.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError('archive exceeds byte limit')
        yield stream


def _check_zip_directory(stream):
    # ZipFile eagerly allocates its entire central directory before infolist().
    # Use its ZIP64-aware trailer parser to bound that allocation first.
    trailer = zipfile._EndRecData(stream)
    if trailer is None:
        raise ValueError('missing ZIP directory')
    if trailer[zipfile._ECD_SIZE] > MAX_DIRECTORY_BYTES or trailer[zipfile._ECD_ENTRIES_TOTAL] > MAX_ARCHIVE_ENTRIES:
        raise ValueError('ZIP directory exceeds archive limits')


def freeze_restore_input(source, destination, size):
    """Copy a size-limited external stream and recognize its frozen ZIP inventory."""
    # Python's ZIP recognition seeks through the trailing end record and its
    # optional 65,535-byte comment. Only give it bounded bytes from a live file.
    trailer_window = 22 + 65_535
    source.seek(max(0, size - trailer_window))
    if not zipfile.is_zipfile(io.BytesIO(source.read(trailer_window))):
        raise ValueError('missing ZIP directory')
    source.seek(0)
    with destination.open('x+b') as output:
        if copy_bounded(source, output, size) != size:
            raise ValueError('archive size changed during submission')
        _check_zip_directory(output)
        try:
            with zipfile.ZipFile(output) as package:
                if 'archive.json' not in package.namelist():
                    raise ValueError('missing archive manifest')
        except (zipfile.BadZipFile, NotImplementedError) as error:
            raise ValueError('invalid ZIP inventory') from error


def _read_members(archive, work):
    with regular_archive(Path(archive).resolve()) as stream:
        _check_zip_directory(stream)
        with zipfile.ZipFile(stream) as source:
            entries = source.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError('too many archive entries')
            inventory = {}
            total = 0
            for entry in entries:
                name = entry.filename
                if (not name or name in ('.', '..') or Path(name).name != name or chr(92) in name
                        or entry.orig_filename != name or name in inventory):
                    raise ValueError('invalid or duplicate archive member')
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or entry.flag_bits & 1:
                    raise ValueError('unsupported archive member encoding')
                limit = min(MAX_MEMBER_BYTES, MAX_MANIFEST_BYTES) if name == 'archive.json' else MAX_MEMBER_BYTES
                total += entry.file_size
                if entry.file_size > limit or total > MAX_EXPANDED_BYTES:
                    raise ValueError('expanded archive exceeds size limit')
                inventory[name] = entry
            if 'archive.json' not in inventory:
                raise ValueError('missing archive manifest')
            entry = inventory.pop('archive.json')
            buffer = io.BytesIO()
            with source.open(entry) as member:
                copied = copy_bounded(member, buffer, entry.file_size)
            if copied != entry.file_size:
                raise ValueError('archive manifest size mismatch')
            manifest = json.loads(buffer.getvalue())
            if not isinstance(manifest, dict) or type(manifest.get('format')) is not int or manifest['format'] != FORMAT:
                raise ValueError('unsupported archive format')
            hashes = manifest.get('sha256')
            if not isinstance(hashes, dict) or hashes.keys() != inventory.keys():
                raise ValueError('archive member inventory mismatch')
            if (not isinstance(manifest.get('decisions'), dict)
                    or type(manifest['decisions'].get('version')) is not int
                    or manifest['decisions']['version'] != 1
                    or not isinstance(manifest['decisions'].get('entries'), dict)):
                raise ValueError('missing or unsupported decision inventory')
            files = {}
            for index, (name, entry) in enumerate(inventory.items()):
                path = work / f'member-{index}'
                digest = hashlib.sha256()
                with source.open(entry) as member, path.open('xb') as target:
                    size = copy_bounded(member, target, entry.file_size, digest)
                if size != entry.file_size or digest.hexdigest() != hashes[name]:
                    raise ValueError('archive integrity mismatch: ' + name)
                files[name] = path
            return manifest, files


def _history_packages(store, ready, *, verify_publication=None):
    packages = {}
    pending = []
    scheduled = set()
    repositories = set()

    def schedule(owner, commit, kind):
        # A restored child can carry its own location map for nested packages.
        # Keep that resolution context when walking the rest of its history.
        repository = (owner.root, owner.base, kind)
        if (repository, commit) in scheduled:
            return
        refs = ('--all',) if repository not in repositories else ()
        repositories.add(repository)
        for oid in git(owner.root, 'rev-list', commit, *refs).splitlines():
            if (repository, oid) not in scheduled:
                scheduled.add((repository, oid))
                pending.append((owner, oid, kind))

    schedule(store, ready, 'atlas')
    while pending:
        owner, commit, kind = pending.pop()
        nodes = owner.repository_closure(commit, kind=kind)
        if verify_publication and kind == 'atlas' and git(owner.root, 'ls-tree', '--name-only', commit, 'publication.json'):
            verify_publication(owner, commit, nodes[0][1]['package_id'])
        for child, node in nodes:
            package = packages.setdefault(node['package_id'], {'path': child.root, 'kind':node['kind'], 'pins': set(), 'receipts': set()})
            if package['kind'] != node['kind']:
                raise ValueError('conflicting archive package roles')
            content = node['commit']
            package['pins'].add((child.root, content))
            schedule(child, content, node['kind'])
            for row in node.get('inclusions', ()):
                wrapper = row['publication']
                package['receipts'].add((wrapper, content))
                # A wrapper can be unreachable from every advertised child ref.
                schedule(child, wrapper, 'atlas')
    return packages


def _verify_archived_publication(store, publication, identity, profile, assets, db):
    snapshot = store.read(publication)
    receipt = Publications(store)._read_receipt(publication, snapshot['manifest'])
    if snapshot['manifest']['package_id'] != identity:
        raise ValueError('publication package identity mismatch')
    previous = receipt['previous_publication']
    if previous and not git(store.root, 'ls-tree', '--name-only', previous, 'publication.json'):
        raise ValueError('previous publication receipt is missing')
    committed_profile = json.loads(git(store.root, 'show', publication + ':embedding-profile.json'))
    if committed_profile != profile or receipt['profile_id'] != profile['profile_id']:
        raise ValueError('archived publication profile mismatch')
    records = records_for(snapshot, receipt['content_commit'])
    chunks = chunks_for(records, *assets)
    expected = {hashlib.sha256(('title: none | text: ' + chunk['text']).encode()).hexdigest()
                for chunk in chunks}
    artifacts = receipt.get('vector_artifacts')
    if (not isinstance(artifacts, dict) or artifacts.keys() != expected
            or type(receipt.get('chunk_count')) is not int or receipt['chunk_count'] != len(chunks)
            or type(receipt.get('record_count')) is not int or receipt['record_count'] != len(records)):
        raise ValueError('incomplete archived vector evidence')
    for digest, artifact in artifacts.items():
        try:
            key = artifact_key(artifact)
        except (KeyError, TypeError, AttributeError, OverflowError) as error:
            raise ValueError('invalid archived vector artifact') from error
        if (digest != artifact['input_sha256'] or artifact['role'] != 'document'
                or artifact['profile'] != profile['profile']):
            raise ValueError('archived document vector mismatch')
        cache_artifact(db, key, artifact)


def _staged_inventory(store, ready, paths, assets, cache):
    if not ready or Publications(store).ready() != ready:
        raise ValueError('archive READY mismatch')
    provider = EmbeddingProvider(assets[0], cache, 'http://127.0.0.1:1')
    with closing(provider.connect()) as db, db:
        def verify(owner, revision, identity):
            _verify_archived_publication(owner, revision, identity, provider.profile, assets, db)
        packages = _history_packages(store, ready, verify_publication=verify)
    if packages.keys() != paths.keys():
        raise ValueError('archive repository inventory mismatch')
    decisions = {}
    for identity, package in packages.items():
        owner = store.at_repository(paths[identity])
        for wrapper, content in sorted(package['receipts']):
            retain_child_publication(owner, wrapper, content)
        if package['kind'] == 'atlas':
            current = Publications(owner).ready()
            if current:
                Publications(owner).read(current, expected_package_id=identity)
            decisions[identity] = decision_inventory(owner)
    return decisions


def _qualified_profile(profile):
    from ..runtime import _assets
    if profile != json.loads((_assets()/'embedding-profile.json').read_text()):
        raise ValueError('qualified archive embedding profile required')


def _merge_copy_decisions(repo, sources):
    """Keep decisions from each local copy of the same UUID in the bundled namespace."""
    def refs(path):
        lines = git(path, 'for-each-ref', '--format=%(refname) %(objectname)', DECISIONS)
        return dict(line.split() for line in lines.splitlines())
    retained = refs(repo)
    for source in sources:
        for ref, oid in refs(source).items():
            if ref in retained:
                if retained[ref] != oid:
                    raise ValueError('conflicting decision refs for the same package UUID')
                continue
            if not GitStore._has_commits(repo, oid):
                git(repo, 'fetch', str(source), oid)
            git(repo, 'update-ref', ref, oid)
            retained[ref] = oid


def export_atlas(store, destination, *, tokenizer, formatter):
    destination=Path(destination)
    if destination.exists():raise ValueError('archive destination exists')
    publications=Publications(store)
    ready=publications.ready()
    if ready is None:raise ValueError('no published atlas to export')
    packages = _history_packages(store, ready)
    root_id=package_identity(store.root, ready)
    profile=json.loads(git(store.root,'show',ready+':embedding-profile.json'))
    _qualified_profile(profile)
    asset_paths = {'tokenizer.json': Path(tokenizer), 'chunking.py': Path(formatter)}
    if len(packages) + len(asset_paths) + 2 > MAX_ARCHIVE_ENTRIES:
        raise ValueError('too many archive entries')
    with tempfile.TemporaryDirectory() as temporary:
        work=Path(temporary);members={};inventory=[]
        paths = {}
        for index,(identity,package) in enumerate(packages.items()):
            repo=work/str(index)
            git(work,'clone','--mirror','--no-hardlinks',str(package['path']),str(repo))
            for number,(source,commit) in enumerate(sorted(package['pins'])):
                if not GitStore._has_commits(repo, commit):
                    git(repo,'fetch',str(source),commit)
                git(repo,'update-ref','refs/poc9/archive-pins/'+str(number),commit)
            copies = {path for path, _ in package['pins']} - {package['path']}
            if copies and package['kind'] == 'atlas':
                _merge_copy_decisions(repo, sorted(copies))
            paths[identity] = repo
            name='root.bundle' if identity==root_id else str(index)+'.bundle'
            inventory.append({'package_id':identity,'bundle':name,
                'head':git(repo,'rev-parse','HEAD')})
        mirror = GitStore(work, archive_locations=paths).at_repository(paths[root_id])
        members.update(asset_paths)
        profile_path = work / 'embedding-profile.json'
        profile_path.write_text(canonical(profile) + '\n', encoding='utf-8')
        members['embedding-profile.json'] = profile_path
        decisions = _staged_inventory(mirror, ready, paths, (profile_path, tokenizer, formatter), work/'vectors')
        for package in inventory:
            name = package['bundle']
            bundle = work/name
            git(paths[package['package_id']], 'bundle', 'create', str(bundle), '--all')
            members[name] = bundle
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent) as staged:
            hashes = {}
            total = 0
            with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, path in members.items():
                    budget = min(MAX_MEMBER_BYTES, MAX_EXPANDED_BYTES - total)
                    if path.stat().st_size > budget:
                        raise ValueError('expanded archive exceeds size limit')
                    digest = hashlib.sha256()
                    with path.open('rb') as source, archive.open(name, 'w') as target:
                        total += copy_bounded(source, target, budget, digest)
                    hashes[name] = digest.hexdigest()
                    if staged.tell() > MAX_ARCHIVE_BYTES:
                        raise ValueError('archive exceeds byte limit')
                if hashes['tokenizer.json'] != profile['profile']['endpoint']['tokenizer_sha256']:
                    raise ValueError('tokenizer digest mismatch')
                if hashes['chunking.py'] != profile['profile']['formatter_sha256']:
                    raise ValueError('formatter digest mismatch')
                manifest = {'format':FORMAT, 'root_package_id':root_id, 'ready':ready, 'packages':inventory,
                            'decisions':{'version':1,'entries':decisions}, 'sha256':hashes}
                metadata = canonical(manifest).encode()
                if len(metadata) > min(MAX_MEMBER_BYTES, MAX_MANIFEST_BYTES, MAX_EXPANDED_BYTES - total):
                    raise ValueError('archive manifest exceeds size limit')
                archive.writestr('archive.json', metadata)
            if staged.tell() > MAX_ARCHIVE_BYTES:
                raise ValueError('archive exceeds byte limit')
            _check_zip_directory(staged)
            # Publish only a complete ZIP; a competing export cannot replace it.
            os.link(staged.name, destination)
    return manifest


def seed_retained_vectors(pubs, db, *, expected_package_id=None):
    current = pubs.ready()
    seen = set()
    while current:
        if current in seen:
            raise ValueError('publication history cycle')
        seen.add(current)
        receipt = pubs.read(current, expected_package_id=expected_package_id)
        for artifact in receipt['vector_artifacts'].values():
            cache_artifact(db, artifact_key(artifact), artifact)
        current = receipt['previous_publication']


def restore_atlas(archive, destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('restore destination exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        work = Path(temporary)
        try:
            manifest, members = _read_members(archive, work)
        except (zipfile.BadZipFile, UnicodeError, RecursionError, EOFError, zlib.error) as error:
            raise ValueError('invalid archive contents') from error
        base=work/'restored';base.mkdir();locations={}
        packages = manifest.get('packages')
        assets_required = {'tokenizer.json', 'chunking.py', 'embedding-profile.json'}
        if not isinstance(packages, list) or not packages or len(packages) > len(members):
            raise ValueError('invalid package inventory')
        bundle_names = set()
        package_ids = set()
        for package in packages:
            if not isinstance(package, dict) or not all(isinstance(package.get(key), str) for key in ('bundle', 'package_id', 'head')):
                raise ValueError('invalid package inventory')
            name = package['bundle']
            if (name in assets_required or name in bundle_names or package['package_id'] in package_ids
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', package['head'])):
                raise ValueError('invalid or duplicate package bundle')
            bundle_names.add(name)
            package_ids.add(package['package_id'])
        if (not isinstance(manifest.get('root_package_id'), str) or manifest['root_package_id'] not in package_ids
                or members.keys() != bundle_names | assets_required):
            raise ValueError('archive package and asset inventory mismatch')
        for index,package in enumerate(packages):
            name = package['bundle']
            bundle = members[name]
            relative='atlas' if package['package_id']==manifest['root_package_id'] else 'modules/'+str(index)
            repo=base/relative;repo.mkdir(parents=True)
            git(repo,'init','-q','-b','restore-unborn')
            heads = git(repo, 'bundle', 'list-heads', str(bundle), 'HEAD').split()
            if heads != [package['head'], 'HEAD']:
                raise ValueError('archive bundle HEAD mismatch')
            git(repo,'fetch',str(bundle),'refs/*:refs/*')
            # Data HEAD is separate from READY: future writes must still use publication CAS.
            branches = git(repo, 'for-each-ref', '--points-at', package['head'],
                           '--format=%(refname:strip=2)', 'refs/heads/').splitlines()
            if 'data' in branches:
                git(repo, 'checkout', 'data')
            else:
                git(repo, 'checkout', '--detach', package['head'])
            identity = package_identity(repo, 'HEAD')
            if identity != package['package_id']:
                raise ValueError('archive package identity mismatch')
            locations[identity] = relative
        _json(base/'package-locations.json',locations)
        assets=base/'assets';assets.mkdir()
        for name in assets_required:
            members[name].rename(assets/name)
        paths = {identity: base / relative for identity, relative in locations.items()}
        store = GitStore(base, archive_locations=paths)
        profile = json.loads((assets/'embedding-profile.json').read_text())
        _qualified_profile(profile)
        if (manifest['sha256']['tokenizer.json'] != profile['profile']['endpoint']['tokenizer_sha256']
                or manifest['sha256']['chunking.py'] != profile['profile']['formatter_sha256']):
            raise ValueError('archive profile asset mismatch')
        decisions = _staged_inventory(store, manifest['ready'], paths,
                                      (assets/'embedding-profile.json', assets/'tokenizer.json', assets/'chunking.py'), base/'vectors')
        if canonical(manifest['decisions']['entries']) != canonical(decisions):
            raise ValueError('archive decision inventory does not match retained objects and refs')
        base.rename(destination)
    return GitStore(destination)


def rebuild_projections(store, builder):
    """Replay PoC9 projection recovery without rewriting canonical receipts."""
    pubs=Publications(store);ready=pubs.ready();current=ready
    chain=[];seen=set()
    while current:
        if current in seen:raise ValueError('publication history cycle')
        seen.add(current);receipt=pubs.read(current);chain.append((current,receipt))
        current=receipt['previous_publication']
    if not chain:raise ValueError('no published history to rebuild')
    projections={}
    for publication,original in reversed(chain):
        projected=builder.build(original['content_commit'])
        for field in ('profile_id','record_count','chunk_count','vector_artifacts'):
            if projected[field]!=original[field]:raise ValueError('rebuilt '+field+' differs from publication')
        projections[publication]=projected
    if pubs.ready()!=ready:raise ValueError('READY changed during recovery; retry with current history')
    path=store.base/'rebuilt-projections.json';pending=path.with_suffix('.pending')
    pending.write_text(json.dumps(projections,ensure_ascii=False,indent=2)+'\n')
    pending.replace(path)
    return {'ready':ready,'publications':len(projections),'mapping':str(path)}
