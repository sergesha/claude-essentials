"""Canonical candidate snapshots; Git history, package pins and raw bytes are data.

Only exact object IDs may be read. A snapshot is not acceptance or publication.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from ..atomic import write_private
from ..storage import FORMAT


def git(repo,*args,binary=False,data=None):
    result=subprocess.run(['git','-C',str(repo),*args],input=data,capture_output=True)
    if result.returncode:
        raise ValueError(f'Git {args[0]} failed (exit {result.returncode})')
    return result.stdout if binary else result.stdout.decode().strip()


def _json(path,value):
    write_private(path,json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')


def package_identity(repo, revision):
    package = json.loads(git(repo, 'show', revision + ':package.json'))
    if (not isinstance(package, dict) or type(package.get('atlas_format')) is not int
            or package['atlas_format'] != FORMAT):
        raise ValueError('unsupported Git package format')
    identity = package.get('package_id')
    if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
        raise ValueError('canonical package UUID required')
    return identity


def project_manifest(repo, revision):
    manifest = json.loads(git(repo, 'show', revision + ':manifest.json'))
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('modules'), dict)
            or manifest.get('status') not in ('candidate', 'accepted')):
        raise ValueError('invalid atlas project manifest')
    return manifest


def _repo(path):
    if not (path/'.git').exists():
        path.mkdir(parents=True,exist_ok=False)
        git(path,'init','-q','-b','data')
        identity = str(uuid.uuid4())
        _json(path/'package.json', {'package_id':identity, 'atlas_format':FORMAT})
        return identity
    identity = package_identity(path, 'HEAD')
    if git(path, 'diff', 'HEAD', '--', 'package.json'):
        raise ValueError('working package identity differs from committed package')
    return identity


def _validate_leaf(repo, oid, kind):
    from .composition import _tree_entries
    tree = _tree_entries(repo, oid)
    required = {'provenance.json'} if kind == 'sources' else {'wiki.json', 'extraction.json', 'evidence.json'}
    if ('manifest.json' in tree or any(tree.get(name, (None,))[0] != '100644' for name in required)
            or any(mode == '160000' for mode, _ in tree.values())):
        raise ValueError('invalid ' + kind + ' content module')


def _commit(path,message,paths=None):
    git(path,'add',*(paths if paths is not None else ['--all']))
    unchanged=subprocess.run(['git','-C',str(path),'diff','--cached','--quiet']).returncode==0
    if not unchanged:
        git(path,'-c','user.name=PoC9 data','-c','user.email=poc9@example.invalid','commit','-qm',message)
    return git(path,'rev-parse','HEAD')


class GitStore:
    def __init__(self,path,*,resolve_atlas=None,archive_locations=None):
        self.resolve_atlas = resolve_atlas
        self.archive_locations = archive_locations
        self.base=Path(path).resolve()
        self.root=self.base/'atlas'

    def at_repository(self, root):
        """Keep this layout's resolver and archive remapping for a child repository."""
        root = Path(root)
        base = self.base
        if root.name == 'atlas' and (root.parent / 'package-locations.json').is_file():
            base = root.parent
        store = GitStore(base, resolve_atlas=self.resolve_atlas, archive_locations=self.archive_locations)
        store.root = root
        return store

    @staticmethod
    def _has_commits(repo, *oids):
        if any(not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid) for oid in oids):
            raise ValueError('exact Git object ID required')
        if not repo.is_dir():
            return False
        objects = git(repo, 'cat-file', '--batch-check=%(objecttype)', data=('\n'.join(oids) + '\n').encode())
        return objects.splitlines() == ['commit'] * len(oids)

    def module_path(self, parent, mod):
        return self._module_store(parent, mod).root

    def _module_store(self, parent, mod):
        if self.archive_locations is not None:
            # During restore every edge must resolve inside the staged bundle inventory.
            try:
                return self.at_repository(self.archive_locations[mod['package_id']])
            except KeyError as error:
                raise ValueError('package missing from archive inventory') from error
        locations=self.base/'package-locations.json'
        mapping=json.loads(locations.read_text()) if locations.exists() else {}
        registered = mod.get('kind') == 'atlas' and self.resolve_atlas is not None
        if mod['package_id'] in mapping:
            mapped = (self.base / mapping[mod['package_id']]).resolve()
            if not registered or self._has_commits(mapped, mod['commit'], mod['publication']):
                return self.at_repository(mapped)
        if registered:
            root = self.resolve_atlas(mod['package_id'])
            # This is a separate registered project. Its own materials use its
            # location map and locators even if the parent has an older copy.
            return GitStore(root.parent, resolve_atlas=self.resolve_atlas)
        if re.match(r'^[^/]+:', mod['url']):
            raise ValueError('registered or archive-local repository required for Git locator')
        return self.at_repository((parent/mod['url']).resolve())

    def closure(self, commit, *, root=None):
        """Resolve pinned packages, preserving every inclusion of an identity."""
        return [node for _, node in self._closure(commit, root=root, by_repository=False)]

    def repository_closure(self, commit, *, kind='atlas'):
        """Return verified owners and their local content/receipt retention obligations."""
        return self._closure(commit, root=None, by_repository=True, kind=kind)

    def _closure(self, commit, *, root, by_repository, kind='atlas'):
        nodes = {}
        versions = {}
        owners = {}

        def visit(owner, path, oid, expected, mount, ancestors, kind, publication=None):
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',oid):
                raise ValueError('exact Git object ID required')
            if not path.is_dir():
                raise ValueError('missing package: '+str(path))
            try:
                if git(path,'cat-file','-t',oid)!='commit':
                    raise ValueError('package pin must be a commit')
                identity=package_identity(path, oid)
            except (ValueError,KeyError) as exc:
                raise ValueError('unavailable package at '+str(path)+': '+str(exc)) from exc
            if path not in owners:
                owners[path] = package_identity(path, 'HEAD')
            if identity != owners[path] or (expected is not None and identity != expected):
                raise ValueError('package identity mismatch')
            if identity in ancestors:
                raise ValueError('package identity cycle: '+identity)
            if versions.setdefault(identity, oid) != oid:
                raise ValueError('conflicting pins for package identity: ' + identity)
            key = (identity, oid, path) if by_repository else (identity, oid)
            if key not in nodes:
                nodes[key] = owner, {'package_id': identity, 'commit': oid, 'path': path, 'mounts': []}
            _, node = nodes[key]
            if by_repository:
                if node.setdefault('kind', kind) != kind:
                    raise ValueError('conflicting package roles')
            if mount not in node['mounts']:node['mounts'].append(mount)
            if publication is not None:
                from .publication import Publications
                Publications(owner).verify_child_receipt(publication, oid)
                node.setdefault('inclusions', []).append({'mount': mount, 'publication': publication})
            if kind != 'atlas':
                _validate_leaf(path, oid, kind)
                return
            manifest=project_manifest(path, oid)
            if manifest['package_id']!=identity:
                raise ValueError('manifest package identity mismatch')
            from .composition import validate_layout
            validate_layout(path, oid, manifest['modules'])
            for name, mod in manifest['modules'].items():
                child = owner._module_store(path, mod)
                inclusion=mount+'/'+mod['mount'] if mount else mod['mount']
                visit(child, child.root, mod['commit'], mod['package_id'], inclusion,
                      ancestors | {identity}, mod.get('kind', name), mod.get('publication'))

        path = self.root if root is None else root
        visit(self.at_repository(path),path,commit,None,'',set(),kind)
        return list(nodes.values())

    def snapshot(self,wiki,extraction,evidence,*,recorded_at,relation_model=None,relation_bundle=None,relation_previous=None):
        from .relations import resolve_relations, validate_bundle
        if datetime.fromisoformat(recorded_at.replace('Z','+00:00')).tzinfo is None:
            raise ValueError('recorded_at requires timezone')
        sources={}
        for s in wiki['sources']:
            name=s['filename']
            if Path(name).name!=name or name in ('','.', '..','package.json'):
                raise ValueError('invalid source name')
            raw=s['content'].encode('utf-8')
            if hashlib.sha256(raw).hexdigest()!=s['sha256']:
                raise ValueError('source digest mismatch: '+name)
            if name in sources:raise ValueError('duplicate source name')
            sources[name]=raw
        if (self.root/'.git').exists():
            previous_snapshot = self.read(git(self.root,'rev-parse','HEAD'))
            if relation_previous is None:
                relation_previous = previous_snapshot.get('relations')
            previous = previous_snapshot['manifest']['modules']
        else:
            previous = {}
        paths = {kind:self.module_path(self.root,previous[kind]) if kind in previous else self.base/'modules'/kind
                 for kind in ('sources','knowledge')}
        for path in paths.values():
            if path.exists():
                if not (path/'.git').exists():
                    raise ValueError('existing content module is not a Git repository')
                _repo(path)
        relations = (validate_bundle(relation_bundle, extraction) if relation_bundle is not None else
                     resolve_relations(extraction, model=relation_model, previous=relation_previous))
        package_id=_repo(self.root)
        # Retain selected independent package pins; only managed contents change.
        modules=dict(previous)
        for kind, path in paths.items():
            identity=_repo(path)
            if kind=='sources':
                rawdir=path/'raw';rawdir.mkdir(exist_ok=True)
                # A new tree may drop a source; previous Git objects preserve it.
                for old in rawdir.iterdir():
                    if old.name not in sources:old.unlink()
                for name,raw in sources.items():(rawdir/name).write_bytes(raw)
                _json(path/'provenance.json',[{k:v for k,v in s.items() if k!='content'} for s in wiki['sources']])
            else:
                _json(path/'wiki.json',wiki)
                _json(path/'extraction.json',extraction)
                _json(path/'relations.json',relations)
                _json(path/'evidence.json',evidence)
            commit=_commit(path,'Retain '+kind+' candidate contents')
            mount=previous[kind]['mount'] if kind in previous else 'packages/'+kind
            url=os.path.relpath(path,self.root)
            git(self.root,'config','-f','.gitmodules','submodule.'+kind+'.path',mount)
            git(self.root,'config','-f','.gitmodules','submodule.'+kind+'.url',url)
            git(self.root,'update-index','--add','--cacheinfo','160000,'+commit+','+mount)
            modules[kind]={'package_id':identity,'commit':commit,'mount':mount,'url':url}
        manifest={'package_id':package_id,'status':'candidate','accepted_at':None,
                  'recorded_at':recorded_at,'modules':modules,'proposal_base_publication':None}
        _json(self.root/'manifest.json',manifest)
        # Replace a previous root-level resolution when compiling new extraction.
        _json(self.root/'relations.json',relations)
        commit=_commit(self.root,'Pin unaccepted source and knowledge packages',['package.json','.gitmodules','manifest.json','relations.json'])
        return {'commit':commit,**manifest}

    def read(self,commit):
        self.closure(commit)
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',commit):
            raise ValueError('exact Git object ID required')
        if git(self.root,'cat-file','-t',commit)!='commit':
            raise ValueError('snapshot must be a commit')
        return self._read_package(self.root,commit)

    def _read_package(self,repo,commit):
        manifest=json.loads(git(repo,'show',commit+':manifest.json'))
        result={'manifest':manifest}
        if git(repo,'ls-tree','--name-only',commit,'knowledge-records.json'):
            result['knowledge_records']=json.loads(git(repo,'show',commit+':knowledge-records.json'))
        included={}
        for kind,mod in manifest['modules'].items():
            oid=mod['commit']
            child = self._module_store(repo, mod)
            path = child.root
            identity=package_identity(path, oid)
            if identity!=mod['package_id']:raise ValueError('package identity mismatch')
            if kind=='sources':
                result['sources']={name[4:]:git(path,'show',oid+':'+name,binary=True)
                    for name in git(path,'ls-tree','-r','--name-only',oid,'raw/').splitlines()}
            elif kind=='knowledge':
                for name in ('wiki','extraction','evidence'):
                    result[name]=json.loads(git(path,'show',oid+':'+name+'.json'))
                if git(path,'ls-tree','--name-only',oid,'relations.json'):
                    result['relations']=json.loads(git(path,'show',oid+':relations.json'))
            elif mod.get('kind')=='atlas':
                if mod.get('scope')!='all':raise ValueError('unsupported package inclusion scope')
                key=(identity,oid)
                if key not in included:
                    included[key]={'package_id':identity,'commit':oid,'scope':'all','mounts':[],
                                   'snapshot':child._read_package(path,oid)}
                included[key]['mounts'].append(mod['mount'])
                included[key].setdefault('inclusions', []).append(
                    {'mount': mod['mount'], 'publication': mod['publication']})
        if git(repo,'ls-tree','--name-only',commit,'relations.json'):
            result['relations']=json.loads(git(repo,'show',commit+':relations.json'))
        if 'relations' in result:
            from .relations import validate_bundle
            validate_bundle(result['relations'], result['extraction'])
        if included:result['included_packages']=list(included.values())
        return result

    def read_package(self,package_id,commit,*,atlas_commit):
        for owner, node in self.repository_closure(atlas_commit):
            if node['package_id'] == package_id and self._has_commits(owner.root, commit):
                snapshot = owner.read(commit)
                if snapshot['manifest']['package_id'] != package_id:
                    raise ValueError('historical package identity mismatch')
                return snapshot
        raise ValueError('package unavailable in selected atlas')
