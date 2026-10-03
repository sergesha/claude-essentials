"""Explicit offline re-embedding. Canonical records are never recreated or deleted.

Stop ALL clients (old clients cannot observe migration locks). A private manifest
binds the database, source identities, absolute expiries and superseded profiles.
Build and verification preserve old vectors. Cleanup is a separate operation.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import redis.asyncio as redis
from embedding import Profile, Embedder
from memory_index import MemoryIndex, mapping, decode, UUID, identity

LOCK = 'emb:migration-lock'
AREA = re.compile(r'(?:mem:|ns:[A-Za-z0-9_-]+:mem:|ns:[A-Za-z0-9_-]*:scope:[A-Za-z0-9_-]{1,128}:mem:)')

class Migration:
    def __init__(self, r, profile, encoder, manifest, *, connection_id):
        self.r, self.profile, self.encoder = r, profile, encoder
        self.path = Path(manifest)
        self.connection_id = connection_id
        self.data = None
        self.owner = ''

    def load(self):
        self.data = json.loads(self.path.read_text())
        if self.data['connection'] != self.connection_id or self.data['profile'] != self.profile.json:
            raise ValueError('Manifest belongs to a different database or target profile')
        return self.data

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name+'.tmp-'+uuid.uuid4().hex)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self.data, f, sort_keys=True, indent=2)
                f.flush();os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @asynccontextmanager
    async def locked(self):
        self.owner = uuid.uuid4().hex
        if not await self.r.set(LOCK, self.owner, nx=True, ex=120):
            raise ValueError('Another migration holds the database lock')
        async def renew():
            while True:
                await asyncio.sleep(20)
                if not await self.r.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('EXPIRE',KEYS[1],120) end return 0",1,LOCK,self.owner):
                    return
        task=asyncio.create_task(renew())
        try:
            yield
            await self.check_lock()
        finally:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            await self.r.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",1,LOCK,self.owner)

    async def check_lock(self):
        if await self.r.get(LOCK) != self.owner.encode():
            raise ValueError('Migration lock lost; do not cut over')

    async def safety(self):
        info=await self.r.info()
        if int(info.get('evicted_keys',0)) != self.data['evictions']:
            raise ValueError('Redis evicted keys during migration; restore/reconcile the backup')
        maximum=int(info.get('maxmemory',0))
        if maximum and info.get('maxmemory_policy') != 'noeviction':
            raise ValueError('Migration requires maxmemory-policy noeviction to protect source records')
        if maximum and maximum-int(info['used_memory']) < max(16*1024**2,maximum//10):
            raise ValueError('Insufficient Redis memory headroom for migration')
        await self.check_lock()

    async def parents(self, prefix):
        found=set()
        async for raw in self.r.scan_iter(prefix+'*',count=200):
            key=decode(raw);mid=key[len(prefix):]
            if UUID.fullmatch(mid) and await self.r.type(key)==b'hash' and await self.r.hexists(key,'text'):
                found.add(mid)
        return found

    def stores(self):
        return [(area,MemoryIndex(self.r,area['prefix'],area['index'],self.profile)) for area in self.data['areas']]

    async def expired(self, expected):
        seconds, micros = await self.r.time()
        return expected['expiry'] >= 0 and seconds * 1000 + micros // 1000 >= expected['expiry']

    def resume_areas(self, discovered):
        self.load()
        recorded = {(a['prefix'], a['index']) for a in self.data['areas']}
        if not set(discovered).issubset(recorded):
            raise ValueError('New memory areas appeared; clients are not quiesced')
        return sorted(recorded)

    async def claim(self, area, store):
        state = {k:decode(v) for k,v in mapping(await self.r.hgetall(store.state)).items()}
        same_run = (state.get('status') == 'building' and state.get('target') == self.profile.fingerprint
                    and state.get('run') == str(self.path.resolve()))
        if state != area['old_state'] and not same_run:
            raise ValueError('Area changed since snapshot or belongs to another migration')
        # Compare the entire state again inside Redis before claiming it.
        await self.r.eval("""
        if redis.call('GET',KEYS[1])~=ARGV[1] then return redis.error_reply('Lock lost') end
        local expected=cjson.decode(ARGV[2])
        local current=redis.call('HGETALL',KEYS[2])
        local n=0 for k,v in pairs(expected) do n=n+1 end
        if #current~=n*2 then return redis.error_reply('State changed') end
        for i=1,#current,2 do
          if expected[current[i]]~=current[i+1] then return redis.error_reply('State changed') end
        end
        redis.call('HSET',KEYS[2],'status','building','owner',ARGV[1],'target',ARGV[3],'run',ARGV[4])
        return 1
        """,2,LOCK,store.state,self.owner,json.dumps(state),self.profile.fingerprint,str(self.path.resolve()))

    async def build(self, areas, *, backup_sha):
        async with self.locked():
            if self.path.exists():
                self.load()
                if self.data['phase'] not in ('building','verified'):
                    raise ValueError('This run has already cut over; use a new manifest for another profile')
                if sorted(areas) != sorted((a['prefix'],a['index']) for a in self.data['areas']):
                    raise ValueError('Resume must select the original areas')
                if backup_sha != self.data['backup_sha']:
                    raise ValueError('Resume requires the original verified backup')
            else:
                self.data={'version':1,'connection':self.connection_id,'profile':self.profile.json,
                           'backup_sha':backup_sha,'phase':'building','evictions':int((await self.r.info('stats'))['evicted_keys']),'areas':[]}
                await self.safety()
                for prefix,index in areas:
                    store=MemoryIndex(self.r,prefix,index,self.profile)
                    state={k:decode(v) for k,v in mapping(await self.r.hgetall(store.state)).items()}
                    if state and state.get('status') != 'active':
                        raise ValueError('Area has an unfinished migration; resume its original manifest')
                    if state.get('profile') == self.profile.fingerprint:
                        raise ValueError('Area already uses this profile; no migration needed')
                    records={}
                    for mid in sorted(await self.parents(prefix)):
                        snap=await store.snapshot(mid)
                        if snap:
                            fields,expiry,content=snap
                            records[mid]={'identity':content,'expiry':expiry}
                    self.data['areas'].append({'prefix':prefix,'index':index,'old_state':state,'records':records})
                self.persist()  # Durable source snapshot BEFORE changing Redis state.
            await self.safety()
            self.data['phase']='building';self.persist()
            for area,store in self.stores():
                await self.check_lock()
                await self.claim(area, store)
                await store.ensure_index()
                for mid,expected in area['records'].items():
                    await self.safety()
                    snap=await store.snapshot(mid)
                    if snap is None:
                        if await self.expired(expected):continue
                        raise ValueError('Unexpired source record disappeared')
                    fields,expiry,content=snap
                    if content!=expected['identity'] or expiry!=expected['expiry']:
                        raise ValueError('Source changed during migration; keep clients stopped')
                    keys=await store.keys_for(mid)
                    if keys and await self.complete(store,mid,expected):continue
                    vectors=await self.encoder.record(decode(fields['text']),decode(fields.get('label',b'')),decode(fields.get('code',b'')))
                    await self.safety()
                    await store.save(mid,fields,vectors,snapshot=snap,owner=self.owner,lock=LOCK)
                self.persist()
            await self._verify()
            self.data['phase']='verified';self.persist()

    async def complete(self, store, mid, expected):
        snap=await store.snapshot(mid)
        if snap is None:return await self.expired(expected)
        _,expiry,content=snap
        if content!=expected['identity'] or expiry!=expected['expiry']:return False
        keys=await store.keys_for(mid)
        if not keys:return False
        for key in keys:
            data=await self.r.hgetall(key)
            if (data.get(b'parent')!=mid.encode() or data.get(b'profile')!=self.profile.fingerprint.encode()
                or len(data.get(b'vector',b''))!=4*self.profile.dimension
                or await self.r.execute_command('PEXPIRETIME',key)!=expiry):return False
        return True

    async def _verify(self):
        await self.safety()
        for area,store in self.stores():
            state={k:decode(v) for k,v in mapping(await self.r.hgetall(store.state)).items()}
            building=(state.get('status')=='building' and state.get('target')==self.profile.fingerprint
                      and state.get('run')==str(self.path.resolve()))
            active=state.get('status')=='active' and state.get('profile')==self.profile.fingerprint
            if not (building or active):
                raise ValueError('Area state belongs to a different profile or migration')
            await store.ensure_index()
            live=await self.parents(area['prefix'])
            if not live.issubset(area['records']):
                raise ValueError('New source records appeared; clients are not quiesced')
            for mid in area['records']:
                if not await self.complete(store,mid,area['records'][mid]):
                    raise ValueError('Missing/changed source or incomplete vector generation')
                # Check actual indexing, not just successful HSETs. Indexing failures must block cutover.
                for key in await store.keys_for(mid):
                    raw=await self.r.execute_command('FT.SEARCH',store.index,'*','INKEYS',1,key,'NOCONTENT','LIMIT',0,1)
                    count = int(raw.get(b'total_results',raw.get('total_results',0))) if isinstance(raw,dict) else int(raw[0])
                    if count != 1:raise ValueError('A derived vector was not indexed')
            info=mapping(await self.r.execute_command('FT.INFO',store.index))
            if int(info.get('hash_indexing_failures',0)):
                raise ValueError('Redis reported vector indexing failures')
        await self.safety()

    async def verify(self):
        self.load()
        async with self.locked():
            await self._verify()
            if self.data['phase'] in ('building','verified'):
                self.data['phase']='verified';self.persist()

    async def cutover(self):
        self.load()
        if self.data['phase'] not in ('verified','cutover'):
            raise ValueError('Build and verify before cutover')
        async with self.locked():
            await self._verify()
            for _,store in self.stores():
                await self.check_lock()
                await self.r.eval("""if redis.call('GET',KEYS[1])~=ARGV[1] then return redis.error_reply('Lock lost') end
                redis.call('HSET',KEYS[2],'status','active','profile',ARGV[2],'config',ARGV[3])
                redis.call('HDEL',KEYS[2],'owner','target','run') return 1""",2,LOCK,store.state,self.owner,self.profile.fingerprint,self.profile.json)
            self.data['phase']='cutover';self.persist()

    async def cleanup(self):
        self.load()
        if self.data['phase'] not in ('cutover','cleaned'):
            raise ValueError('Cut over before cleanup')
        async with self.locked():
            for area,store in self.stores():
                await store.ensure()
                old=area['old_state'].get('profile')
                oldindex=area['index']+':emb:'+old if old else area['index']
                oldprefix=area['prefix'][:-4]+'emb:'+old+':' if old else area['prefix']
                try:
                    info=mapping(await self.r.execute_command('FT.INFO',oldindex))
                except redis.ResponseError as exc:
                    if 'unknown index name' not in str(exc).lower():raise
                else:
                    definition=mapping(info['index_definition'])
                    if [decode(x) for x in definition.get('prefixes',[])]!=[oldprefix]:
                        raise ValueError('Refusing cleanup of an index outside the recorded old area')
                    await self.cleanup_command(store, 'FT.DROPINDEX', oldindex)  # NEVER DD.
                for mid in area['records']:
                    await self.check_lock()
                    if old:
                        keys=[k async for k in self.r.scan_iter(oldprefix+mid+':*',count=200)]
                        if keys:await self.cleanup_command(store, 'DEL', *keys)
                    key=area['prefix']+mid
                    if await self.r.exists(key):
                        if not await self.r.hexists(key,store.generation_field):
                            raise ValueError('Refusing cleanup: live parent has no new generation')
                        await self.cleanup_command(store, 'HDEL', key,*(['_emb_'+old,'_chunks_'+old] if old else ['vector']))
            self.data['phase']='cleaned';self.persist()

    async def cleanup_command(self, store, command, *arguments):
        return await self.r.eval("""
        if redis.call('GET',KEYS[1])~=ARGV[1] then return redis.error_reply('Lock lost') end
        if redis.call('HGET',KEYS[2],'status')~='active' or
           redis.call('HGET',KEYS[2],'profile')~=ARGV[2] then
          return redis.error_reply('Active profile changed')
        end
        return redis.call(ARGV[3],unpack(ARGV,4))
        """,2,LOCK,store.state,self.owner,self.profile.fingerprint,command,*arguments)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['inspect','build','verify','cutover','cleanup'])
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--all',action='store_true',help='Administrative scan of all recognized memory areas')
    parser.add_argument('--shared',action='store_true')
    parser.add_argument('--scope')
    parser.add_argument('--backup',type=Path,help='Original Redis RDB backup, preserved for resume/rollback')
    parser.add_argument('--backup-verified',action='store_true',help='Attest this backup has been successfully restored and checked')
    parser.add_argument('--quiesced',action='store_true',help='Attest ALL old and new clients are stopped')
    args=parser.parse_args()
    if args.phase!='inspect' and (not args.manifest or not args.quiesced or not args.backup_verified or not args.backup):
        parser.error('Mutations/verification require --manifest --quiesced --backup FILE --backup-verified')
    async def run():
        from memory_mcp import _scope, _BASE_INDEX, REDIS_URL, EMBED_URL, EMBED_SOCKET
        profile=Profile.from_env();r=redis.from_url(REDIS_URL,protocol=2)
        encoder=Embedder(profile,EMBED_URL,EMBED_SOCKET)
        try:
            areas={}
            if args.all:
                async for raw in r.scan_iter('*mem:*',count=200):
                    key=decode(raw);prefix,sep,mid=key.rpartition(':');prefix+=':'
                    if not sep or not UUID.fullmatch(mid) or not AREA.fullmatch(prefix):continue
                    if await r.type(key)!=b'hash' or not await r.hexists(key,'text'):continue
                    if ':scope:' in prefix:
                        namespace,scope=prefix[3:-4].split(':scope:');index=f'{_BASE_INDEX}:scope:{namespace}:{scope.rstrip(":")}'
                    elif prefix.startswith('ns:'):index=_BASE_INDEX+':'+prefix[3:-5]
                    else:index=_BASE_INDEX
                    areas[prefix]=index
            else:
                prefix,_,index=_scope(args.shared,args.scope);areas[prefix]=index
            if args.phase=='inspect':
                counts=[]
                for prefix in areas:
                    parents=set()
                    async for raw in r.scan_iter(prefix+'*',count=200):
                        key=decode(raw)
                        if (UUID.fullmatch(key[len(prefix):]) and await r.type(key)==b'hash'
                                and await r.hexists(key,'text')):
                            parents.add(key)
                    counts.append(len(parents))
                print(json.dumps({'areas':len(areas),'records':sum(counts),'target_profile':profile.fingerprint,'config':json.loads(profile.json)}))
                return
            with args.backup.open('rb') as f:
                if f.read(5)!=b'REDIS':raise ValueError('Backup is not a Redis RDB')
                f.seek(0);backup_sha=hashlib.file_digest(f,'sha256').hexdigest()
            migration=Migration(r,profile,encoder,args.manifest,connection_id=hashlib.sha256(REDIS_URL.encode()).hexdigest())
            if args.all and args.manifest.exists():
                areas=dict(migration.resume_areas(areas.items()))
            if args.phase=='build':await migration.build(sorted(areas.items()),backup_sha=backup_sha)
            else:
                data=migration.load()
                if data['backup_sha']!=backup_sha:raise ValueError('Backup differs from migration manifest')
                await getattr(migration,args.phase)()
            print(json.dumps({'phase':migration.data['phase'],'areas':len(migration.data['areas'])}))
        finally:
            await encoder.aclose();await r.aclose()
    try:asyncio.run(run())
    except Exception as exc:
        # Exceptions can contain credentials/key names: never print their raw text.
        parser.exit(1,f'Migration failed ({type(exc).__name__}); keep clients stopped and inspect the private manifest.\n')

if __name__=='__main__':main()
