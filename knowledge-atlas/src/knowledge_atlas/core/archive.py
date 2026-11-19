"""Portable Git package closure and retained document vectors.

Bundles preserve canonical commits; the restored provider cache is disposable.
Native graph/search projections must subsequently be rebuilt.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile
from .git_store import GitStore, git, _json
from .publication import Publications
from .decision_receipts import decision_inventory
from .embedding_provider import EmbeddingProvider, canonical, raw_vectors, cache_artifact


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
    assets={'tokenizer.json':Path(tokenizer).read_bytes(),'chunking.py':Path(formatter).read_bytes(),
            'embedding-profile.json':(canonical(profile)+'\n').encode()}
    if hashlib.sha256(assets['tokenizer.json']).hexdigest()!=profile['profile']['endpoint']['tokenizer_sha256']:
        raise ValueError('tokenizer digest mismatch')
    if hashlib.sha256(assets['chunking.py']).hexdigest()!=profile['profile']['formatter_sha256']:
        raise ValueError('formatter digest mismatch')
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
            members[name]=bundle.read_bytes()
            inventory.append({'package_id':identity,'bundle':name,
                'head':git(package['path'],'rev-parse','HEAD')})
        members.update(assets)
        manifest={'format':2,'root_package_id':root_id,'ready':ready,'packages':inventory,
                  'decisions':{'version':1,'entries':decisions},
                  'sha256':{name:hashlib.sha256(data).hexdigest() for name,data in members.items()}}
        destination.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,data in members.items():archive.writestr(name,data)
            archive.writestr('archive.json',canonical(manifest))
    return manifest


def restore_atlas(archive, destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('restore destination exists')
    with zipfile.ZipFile(archive) as source:
        try:manifest=json.loads(source.read('archive.json'))
        except KeyError as exc:raise ValueError('missing archive manifest') from exc
        if manifest.get('format') not in (1,2):raise ValueError('unsupported archive format')
        if manifest['format']==2 and (not isinstance(manifest.get('decisions'),dict) or manifest['decisions'].get('version')!=1):
            raise ValueError('missing or unsupported decision inventory')
        members={}
        for name,digest in manifest['sha256'].items():
            if Path(name).name!=name:raise ValueError('invalid archive member')
            try:data=source.read(name)
            except KeyError as exc:raise ValueError('missing archive member: '+name) from exc
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('archive integrity mismatch: '+name)
            members[name]=data
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        work=Path(temporary);base=work/'restored';base.mkdir();locations={}
        for index,package in enumerate(manifest['packages']):
            name=package['bundle']
            if name not in members:raise ValueError('missing package bundle')
            bundle=work/(str(index)+'.bundle');bundle.write_bytes(members[name])
            relative='atlas' if package['package_id']==manifest['root_package_id'] else 'modules/'+str(index)
            repo=base/relative;repo.mkdir(parents=True)
            git(repo,'init','-q','-b','restore-unborn')
            git(repo,'fetch',str(bundle),'refs/*:refs/*')
            # Data HEAD is separate from READY: future writes must still use publication CAS.
            git(repo,'checkout','-B','data',package['head'])
            locations[package['package_id']]=relative
        _json(base/'package-locations.json',locations)
        assets=base/'assets';assets.mkdir()
        for name in ('tokenizer.json','chunking.py','embedding-profile.json'):
            if name not in members:raise ValueError('missing runtime asset: '+name)
            (assets/name).write_bytes(members[name])
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
