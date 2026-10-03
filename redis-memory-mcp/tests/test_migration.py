import sys,uuid,os,struct,time
from pathlib import Path
import pytest
import redis.asyncio as redis
sys.path.insert(0,str(Path(__file__).parents[1]/'server'))

@pytest.mark.asyncio
async def test_offline_migration_preserves_sources_expiry_and_cleanup(tmp_path):
    import migrate
    from embedding import Profile
    from memory_index import MemoryIndex
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    r=redis.from_url(url,protocol=2);root='ns:migration_'+uuid.uuid4().hex+':'
    prefix=root+'mem:';base=root+'idx';mid=str(uuid.uuid4());key=prefix+mid
    await r.hset(key,mapping={'text':'original','code':'x=1','label':'label','tags':'test','timestamp':'123','ttl_days':'7','vector':b'legacy'})
    expiry=int(time.time()*1000)+3600000;await r.pexpireat(key,expiry)
    await r.set(root+'kv:unchanged','value')
    class Encoder:
        async def record(self,*args):return [struct.pack('<256f',1,*([0]*255))]*2
    manifest=tmp_path/'run.json';store=MemoryIndex(r,prefix,base,Profile())
    try:
        migration=migrate.Migration(r,Profile(),Encoder(),manifest,connection_id='test')
        await migration.build([(prefix,base)],backup_sha='verified-fixture')
        assert await r.hget(key,'vector')==b'legacy'
        assert await r.execute_command('PEXPIRETIME',key)==expiry
        await migration.verify()
        await migration.cutover()
        hits=await store.search(struct.pack('<256f',1,*([0]*255)),'',5)
        assert hits[0][0]==mid and hits[0][1]['text']=='original'
        await migration.cleanup()
        assert await r.hget(key,'vector') is None
        assert await r.hget(key,'text')==b'original'
        assert await r.get(root+'kv:unchanged')==b'value'
        await migration.cleanup()
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(root+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['source','expiry','chunk','profile'])
async def test_verification_blocks_changed_or_incomplete_build(tmp_path, fault):
    import migrate
    from embedding import Profile
    from memory_index import MemoryIndex
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    r=redis.from_url(url,protocol=2);root='ns:fault_'+uuid.uuid4().hex+':'
    prefix=root+'mem:';base=root+'idx';mid=str(uuid.uuid4());key=prefix+mid
    await r.hset(key,mapping={'text':'source','ttl_days':'0','vector':b'legacy'})
    class Encoder:
        async def record(self,*args):return [struct.pack('<256f',1,*([0]*255))]
    migration=migrate.Migration(r,Profile(),Encoder(),tmp_path/'run.json',connection_id='test')
    store=MemoryIndex(r,prefix,base,Profile())
    try:
        await migration.build([(prefix,base)],backup_sha='backup')
        if fault=='source':await r.hset(key,'text','changed')
        elif fault=='expiry':await r.expire(key,60)
        elif fault=='chunk':await r.delete(*(await store.keys_for(mid)))
        else:await r.hset(store.state,mapping={'status':'active','profile':'other-profile'})
        with pytest.raises(ValueError):await migration.cutover()
        assert await r.hget(key,'vector')==b'legacy'
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(root+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['stale_resume','deleted_permanent'])
async def test_resume_never_overwrites_newer_state_or_loses_permanent_source(tmp_path,fault):
    import migrate
    from embedding import Profile
    from memory_index import MemoryIndex
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    r=redis.from_url(url,protocol=2);root='ns:resume_'+uuid.uuid4().hex+':'
    prefix=root+'mem:';base=root+'idx';mid=str(uuid.uuid4());key=prefix+mid
    await r.hset(key,mapping={'text':'source','ttl_days':'0','vector':b'old'})
    class Encoder:
        async def record(self,*a):return [struct.pack('<256f',1,*([0]*255))]
    m=migrate.Migration(r,Profile(),Encoder(),tmp_path/'run.json',connection_id='test');s=MemoryIndex(r,prefix,base,Profile())
    try:
        await m.build([(prefix,base)],backup_sha='backup')
        if fault=='stale_resume':
            await r.hset(s.state,mapping={'status':'active','profile':'newer-profile'})
            with pytest.raises(ValueError):await m.build([(prefix,base)],backup_sha='backup')
            assert await r.hget(s.state,'profile')==b'newer-profile'
        else:
            await r.delete(key)
            with pytest.raises(ValueError):await m.cutover()
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(root+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()

@pytest.mark.asyncio
async def test_expired_area_resume_and_cleanup_lease_fencing(tmp_path):
    import migrate
    from embedding import Profile
    from memory_index import MemoryIndex
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    r=redis.from_url(url,protocol=2);root='ns:lease_'+uuid.uuid4().hex+':'
    prefix=root+'mem:';base=root+'idx';mid=str(uuid.uuid4());key=prefix+mid
    await r.hset(key,mapping={'text':'source','ttl_days':'0','vector':b'old'})
    class Encoder:
        async def record(self,*a):return [struct.pack('<256f',1,*([0]*255))]
    m=migrate.Migration(r,Profile(),Encoder(),tmp_path/'run.json',connection_id='test');s=MemoryIndex(r,prefix,base,Profile())
    try:
        await m.build([(prefix,base)],backup_sha='backup')
        assert m.resume_areas([])==[(prefix,base)]
        with pytest.raises(ValueError):m.resume_areas([('ns:unexpected:mem:','unexpected')])
        await m.cutover()
        async with m.locked():
            await r.set(migrate.LOCK,'other-owner',ex=120)
            with pytest.raises(redis.ResponseError,match='Lock lost'):
                await m.cleanup_command(s,'HDEL',key,'vector')
            assert await r.hget(key,'vector')==b'old'
            await r.set(migrate.LOCK,m.owner,ex=120)
            await r.hset(s.state,'profile','different')
            with pytest.raises(redis.ResponseError,match='Active profile changed'):
                await m.cleanup_command(s,'HDEL',key,'vector')
            assert await r.hget(key,'vector')==b'old'
        # Natural expiry is permitted; permanent/unexpired deletion is not.
        await r.delete(key)
        seconds,micros=await r.time()
        assert await m.complete(s,mid,{'expiry':seconds*1000+micros//1000-1,'identity':'old'})
        assert not await m.complete(s,mid,{'expiry':-1,'identity':'old'})
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(root+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()

@pytest.mark.asyncio
async def test_dimension_change_migration_removes_only_old_generation(tmp_path):
    from migrate import Migration
    from embedding import Profile
    from memory_index import MemoryIndex
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    r=redis.from_url(url,protocol=2);root='ns:dimension_'+uuid.uuid4().hex+':'
    prefix=root+'mem:';base=root+'idx';mid=str(uuid.uuid4())
    old=MemoryIndex(r,prefix,base,Profile());new_profile=Profile(dimension=128)
    new=MemoryIndex(r,prefix,base,new_profile)
    class Encoder:
        async def record(self,*a):return [struct.pack('<128f',1,*([0]*127))]
    try:
        await old.ensure(create=True)
        await old.save(mid,{'text':'preserved','ttl_days':'0'},[struct.pack('<256f',1,*([0]*255))])
        oldkeys=await old.keys_for(mid)
        m=Migration(r,new_profile,Encoder(),tmp_path/'dimension.json',connection_id='test')
        await m.build([(prefix,base)],backup_sha='backup');await m.cutover();await m.cleanup()
        assert not any([await r.exists(k) for k in oldkeys])
        hits=await new.search(struct.pack('<128f',1,*([0]*127)),'',5)
        assert hits[0][0]==mid and hits[0][1]['text']=='preserved'
        assert await r.execute_command('PEXPIRETIME',prefix+mid)==-1
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(root+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('info,reason',[
    ({'evicted_keys':1},'evicted'),
    ({'maxmemory':64*1024**2,'maxmemory_policy':'volatile-lru','used_memory':1},'noeviction'),
    ({'maxmemory':64*1024**2,'maxmemory_policy':'noeviction','used_memory':60*1024**2},'headroom'),
])
async def test_migration_safety_refuses_eviction_and_memory_pressure(tmp_path,info,reason):
    from migrate import Migration
    from embedding import Profile
    class Redis:
        async def info(self):return info
    m=Migration(Redis(),Profile(),None,tmp_path/'run',connection_id='test');m.data={'evictions':0}
    with pytest.raises(ValueError,match=reason):await m.safety()

@pytest.mark.asyncio
async def test_interrupted_build_resumes_without_reencoding_completed_parents(tmp_path):
    from migrate import Migration,LOCK
    from embedding import Profile
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    r=redis.from_url(url,protocol=2);root='ns:interrupt_'+uuid.uuid4().hex+':'
    prefix=root+'mem:';base=root+'idx'
    for _ in range(2):await r.hset(prefix+str(uuid.uuid4()),mapping={'text':'source','ttl_days':'0','vector':b'old'})
    class Encoder:
        calls=0
        fail=True
        async def record(self,*a):
            self.calls+=1
            if self.fail and self.calls==2:raise RuntimeError('injected embedding outage')
            return [struct.pack('<256f',1,*([0]*255))]
    e=Encoder();m=Migration(r,Profile(),e,tmp_path/'run.json',connection_id='test')
    try:
        with pytest.raises(RuntimeError):await m.build([(prefix,base)],backup_sha='backup')
        assert not await r.exists(LOCK)
        e.fail=False
        await m.build([(prefix,base)],backup_sha='backup')
        assert e.calls==3
        await m.cutover();await m.cleanup()
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(root+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()
