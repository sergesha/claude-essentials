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
import tempfile
import uuid
from ..atomic import write_private


def git(repo,*args,binary=False,data=None):
    result=subprocess.run(['git','-C',str(repo),*args],input=data,capture_output=True)
    if result.returncode:
        raise ValueError(f'Git {args[0]} failed (exit {result.returncode})')
    return result.stdout if binary else result.stdout.decode().strip()


def _json(path,value):
    write_private(path,json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')


def _repo(path):
    if not (path/'.git').exists():
        path.mkdir(parents=True,exist_ok=False)
        git(path,'init','-q','-b','data')
        _json(path/'package.json',{'package_id':str(uuid.uuid4())})
    return json.loads((path/'package.json').read_text())['package_id']


def _commit(path,message,paths=None):
    git(path,'add',*(paths if paths is not None else ['--all']))
    unchanged=subprocess.run(['git','-C',str(path),'diff','--cached','--quiet']).returncode==0
    if not unchanged:
        git(path,'-c','user.name=PoC9 data','-c','user.email=poc9@example.invalid','commit','-qm',message)
    return git(path,'rev-parse','HEAD')


class GitStore:
    def __init__(self,path,*,resolve_atlas=None):
        self.resolve_atlas = resolve_atlas
        self.base=Path(path).resolve()
        self.root=self.base/'atlas'

    def at_repository(self, root):
        """Keep this layout's resolver and archive remapping for a child repository."""
        root = Path(root)
        base = self.base
        if root.name == 'atlas' and (root.parent / 'package-locations.json').is_file():
            base = root.parent
        store = GitStore(base, resolve_atlas=self.resolve_atlas)
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
        locations=self.base/'package-locations.json'
        mapping=json.loads(locations.read_text()) if locations.exists() else {}
        registered = mod.get('kind') == 'atlas' and 'publication' in mod and self.resolve_atlas is not None
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

    def repository_closure(self, commit):
        """Return verified owners and their local content/receipt retention obligations."""
        return self._closure(commit, root=None, by_repository=True)

    def _closure(self, commit, *, root, by_repository):
        nodes = {}
        versions = {}

        def visit(owner, path, oid, expected, mount, ancestors, publication=None):
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',oid):
                raise ValueError('exact Git object ID required')
            if not path.is_dir():
                raise ValueError('missing package: '+str(path))
            try:
                if git(path,'cat-file','-t',oid)!='commit':
                    raise ValueError('package pin must be a commit')
                identity=json.loads(git(path,'show',oid+':package.json'))['package_id']
            except (ValueError,KeyError) as exc:
                raise ValueError('unavailable package at '+str(path)+': '+str(exc)) from exc
            if expected is not None and identity!=expected:
                raise ValueError('package identity mismatch')
            if identity in ancestors:
                raise ValueError('package identity cycle: '+identity)
            if versions.setdefault(identity, oid) != oid:
                raise ValueError('conflicting pins for package identity: ' + identity)
            key = (identity, oid, path) if by_repository else (identity, oid)
            if key not in nodes:
                nodes[key] = owner, {'package_id': identity, 'commit': oid, 'path': path, 'mounts': []}
            _, node = nodes[key]
            if mount not in node['mounts']:node['mounts'].append(mount)
            if publication is not None:
                from .publication import Publications
                Publications(owner).verify_child_receipt(publication, oid)
                node.setdefault('inclusions', []).append({'mount': mount, 'publication': publication})
            if not git(path,'ls-tree','--name-only',oid,'manifest.json'):
                return
            manifest=json.loads(git(path,'show',oid+':manifest.json'))
            if manifest['package_id']!=identity:
                raise ValueError('manifest package identity mismatch')
            from .composition import validate_layout
            validate_layout(path, oid, manifest['modules'])
            for mod in manifest['modules'].values():
                child = owner._module_store(path, mod)
                inclusion=mount+'/'+mod['mount'] if mount else mod['mount']
                visit(child, child.root, mod['commit'], mod['package_id'], inclusion,
                      ancestors | {identity}, mod.get('publication'))

        path = self.root if root is None else root
        visit(self.at_repository(path),path,commit,None,'',set())
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
        if relation_previous is None and (self.root/'manifest.json').exists():
            relation_previous=self.read(git(self.root,'rev-parse','HEAD')).get('relations')
        relations = (validate_bundle(relation_bundle, extraction) if relation_bundle is not None else
                     resolve_relations(extraction, model=relation_model, previous=relation_previous))
        package_id=_repo(self.root)
        previous_path=self.root/'manifest.json'
        previous=json.loads(previous_path.read_text())['modules'] if previous_path.exists() else {}
        # Retain selected independent package pins; only managed contents change.
        modules=dict(previous)
        for kind in ('sources','knowledge'):
            path=self.module_path(self.root,previous[kind]) if kind in previous else self.base/'modules'/kind
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

    def pin_package(self,base,child,child_commit,*,mount,recorded_at,base_publication=None):
        """Propose an exact independent package pin; acceptance is separate."""
        from .publication import Publications,commit_files
        if (not isinstance(mount,str) or not mount or mount.startswith('/')
                or any(part in ('','.','..','.git') for part in mount.split('/'))):
            raise ValueError('relative package mount required')
        if datetime.fromisoformat(recorded_at.replace('Z','+00:00')).tzinfo is None:
            raise ValueError('recorded_at requires timezone')
        snapshot=self.read(base);manifest=snapshot['manifest'];pubs=Publications(self)
        if pubs.ready()!=base_publication:raise ValueError('stale package proposal')
        origin=pubs.read(base_publication)['content_commit'] if base_publication else manifest.get('proposal_base_content',base)
        if base_publication and base!=origin:
            if (manifest['status']!='candidate' or manifest.get('proposal_base_content')!=origin
                    or manifest.get('proposal_base_publication')!=base_publication):
                raise ValueError('package proposal must derive from selected publication')
            git(self.root,'merge-base','--is-ancestor',origin,base)
        child_snapshot=child.read(child_commit)
        if manifest['package_id'] in {n['package_id'] for n in child.closure(child_commit)}:
            raise ValueError('package identity cycle')
        modules=dict(manifest['modules'])
        for name,mod in modules.items():
            if mod['mount']==mount and mod.get('kind')!='atlas':
                raise ValueError('mount already belongs to a content module')
            if mod['mount']!=mount and (mount.startswith(mod['mount']+'/') or mod['mount'].startswith(mount+'/')):
                raise ValueError('overlapping package mounts')
        name='included-'+hashlib.sha256(mount.encode()).hexdigest()[:24]
        modules[name]={'package_id':child_snapshot['manifest']['package_id'],'commit':child_commit,
            'mount':mount,'url':os.path.relpath(child.root,self.root),'kind':'atlas','scope':'all'}
        manifest={**manifest,'modules':modules,'status':'candidate','accepted_at':None,'recorded_at':recorded_at,
                  'proposal_base_content':origin,'proposal_base_publication':base_publication}
        manifest.pop('acceptance',None)
        with tempfile.TemporaryDirectory() as temporary:
            config=Path(temporary)/'gitmodules'
            config.write_bytes(git(self.root,'show',base+':.gitmodules',binary=True)
                if git(self.root,'ls-tree','--name-only',base,'.gitmodules') else b'')
            git(self.root,'config','-f',str(config),'submodule.'+name+'.path',mount)
            git(self.root,'config','-f',str(config),'submodule.'+name+'.url',modules[name]['url'])
            candidate=commit_files(self.root,base,{'manifest.json':manifest,'.gitmodules':config.read_bytes()},
                [base],'Propose independent package pin',gitlinks={mount:child_commit})
        self.read(candidate)
        git(self.root,'update-ref','refs/poc9/proposals/'+candidate,candidate)
        return candidate

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
            identity=json.loads(git(path,'show',oid+':package.json'))['package_id']
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
                if 'publication' in mod:
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
