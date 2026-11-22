"""Portable Git package closure and retained document vectors.

Bundles preserve canonical commits; the restored provider cache is disposable.
Native graph/search projections must subsequently be rebuilt.
"""
from contextlib import contextmanager
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
from .git_store import GitStore, git, _json
from .publication import Publications
from .decision_receipts import decision_inventory
from .embedding_provider import EmbeddingProvider, canonical, raw_vectors, cache_artifact


# Limits apply to both supported archive formats, including archive.json.
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
            if not isinstance(manifest, dict) or manifest.get('format') not in (1, 2):
                raise ValueError('unsupported archive format')
            hashes = manifest.get('sha256')
            if not isinstance(hashes, dict) or hashes.keys() != inventory.keys():
                raise ValueError('archive member inventory mismatch')
            if manifest['format'] == 2 and (not isinstance(manifest.get('decisions'), dict)
                    or manifest['decisions'].get('version') != 1):
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


def export_atlas(store, destination, *, tokenizer, formatter):
    destination=Path(destination)
    if destination.exists():raise ValueError('archive destination exists')
    publications=Publications(store)
    ready=publications.ready()
    if ready is None:raise ValueError('no published atlas to export')
    decisions=decision_inventory(store)
    packages={};pending=[];scheduled=set();repositories=set()
    def retain_history(path,commit):
        refs=('--all',) if path not in repositories else ()
        repositories.add(path)
        for oid in git(path,'rev-list',commit,*refs).splitlines():
            if (path,oid) not in scheduled:
                scheduled.add((path,oid));pending.append((path,oid))
    retain_history(store.root,ready)
    while pending:
        path,commit=pending.pop()
        for node in store.closure(commit,root=path):
            package=packages.setdefault(node['package_id'],{'path':node['path'],'pins':set()})
            package['pins'].add((node['path'],node['commit']))
            if (node['path'],node['commit']) not in scheduled:
                retain_history(node['path'],node['commit'])
    root_id=json.loads(git(store.root,'show',ready+':package.json'))['package_id']
    profile=json.loads(git(store.root,'show',ready+':embedding-profile.json'))
    asset_paths = {'tokenizer.json': Path(tokenizer), 'chunking.py': Path(formatter)}
    if len(packages) + len(asset_paths) + 2 > MAX_ARCHIVE_ENTRIES:
        raise ValueError('too many archive entries')
    with tempfile.TemporaryDirectory() as temporary:
        work=Path(temporary);members={};inventory=[]
        for index,(identity,package) in enumerate(packages.items()):
            repo=work/str(index)
            git(work,'clone','--mirror','--no-hardlinks',str(package['path']),str(repo))
            for number,(source,commit) in enumerate(sorted(package['pins'])):
                git(repo,'fetch',str(source),commit)
                git(repo,'update-ref','refs/poc9/archive-pins/'+str(number),commit)
            name='root.bundle' if identity==root_id else str(index)+'.bundle'
            bundle=work/name
            git(repo,'bundle','create',str(bundle),'--all')
            members[name]=bundle
            inventory.append({'package_id':identity,'bundle':name,
                'head':git(package['path'],'rev-parse','HEAD')})
        members.update(asset_paths)
        profile_path = work / 'embedding-profile.json'
        profile_path.write_text(canonical(profile) + '\n', encoding='utf-8')
        members['embedding-profile.json'] = profile_path
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
                manifest = {'format':2, 'root_package_id':root_id, 'ready':ready, 'packages':inventory,
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
            git(repo,'fetch',str(bundle),'refs/*:refs/*')
            # Data HEAD is separate from READY: future writes must still use publication CAS.
            git(repo,'checkout','-B','data',package['head'])
            locations[package['package_id']]=relative
        _json(base/'package-locations.json',locations)
        assets=base/'assets';assets.mkdir()
        for name in assets_required:
            members[name].rename(assets/name)
        store=GitStore(base);pubs=Publications(store)
        if pubs.ready()!=manifest['ready']:raise ValueError('archive READY mismatch')
        provider=EmbeddingProvider(assets/'embedding-profile.json',base/'vectors','http://127.0.0.1:1')
        current=pubs.ready();seen=set()
        with provider.connect() as db:
            while current:
                if current in seen:raise ValueError('publication history cycle')
                seen.add(current);receipt=pubs.read(current)
                for artifact in receipt['vector_artifacts'].values():
                    profile_id=hashlib.sha256(canonical(artifact['profile']).encode()).hexdigest()
                    if artifact['profile_id']!=profile_id or artifact['role'] not in ('document','query'):
                        raise ValueError('vector artifact identity mismatch')
                    if hashlib.sha256(artifact['input'].encode()).hexdigest()!=artifact['input_sha256']:
                        raise ValueError('vector input digest mismatch')
                    raw_vectors([artifact['raw768']],1)
                    key=hashlib.sha256(canonical([profile_id,artifact['role'],artifact['input']]).encode()).hexdigest()
                    # Archive members and Git receipts have already been verified.
                    # Integrity metadata is local; canonical artifact bytes stay unchanged.
                    cache_artifact(db,key,artifact)
                current=receipt['previous_publication']
        # Validate all retained pins, including unpublished proposals, before exposing restore.
        for commit in git(store.root,'rev-list','--all').splitlines():store.closure(commit)
        decisions=decision_inventory(store)
        if manifest['format']==2 and manifest['decisions'].get('entries')!=decisions:
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
