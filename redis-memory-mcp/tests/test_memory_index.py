import importlib
import os
from pathlib import Path
import struct
import sys
import time
import uuid
import pytest
import pytest_asyncio
import redis.asyncio as redis
sys.path.insert(0, str(Path(__file__).parents[1] / 'server'))
from embedding import Profile

@pytest_asyncio.fixture
async def store():
    url = os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:
        pytest.skip('Set REDIS_GRANITE_TEST_URL to an isolated Redis Stack')
    r = redis.from_url(url, decode_responses=False, protocol=2)
    prefix = 'ns:test_' + uuid.uuid4().hex + ':mem:'
    base = 'idx:' + uuid.uuid4().hex
    yield r, prefix, base
    for key in [k async for k in r.scan_iter(prefix[:-4]+'*')]:
        await r.delete(key)
    try:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(base):await r.execute_command('FT.DROPINDEX', index)
    except redis.ResponseError:
        pass
    await r.aclose()


def build(fixture):
    r, prefix, base = fixture
    return importlib.import_module('memory_index').MemoryIndex(r, prefix, base, Profile())

def vector(i=0):
    v = [0.0]*256;v[i] = 1.0
    return struct.pack('<256f', *v)

@pytest.mark.asyncio
async def test_complete_parent_grouping_shared_expiry_and_delete(store):
    store = build(store)
    await store.ensure(create=True)
    ids = []
    for i in range(7):
        mid = str(uuid.uuid4());ids.append(mid)
        await store.save(mid, {'text':'full text '+str(i),'code':'print(1)','timestamp':str(i),'ttl_days':'1','tags':'fixture'}, [vector()]*8)
    hits = await store.search(vector(), '', 5)
    assert len(hits) == len({h[0] for h in hits}) == 5
    for mid, fields, score, ttl in hits:
        assert fields['text'].startswith('full text ') and fields['code']=='print(1)'
        keys = await store.keys_for(mid)
        expiry = await store.r.execute_command('PEXPIRETIME', store.mem_prefix+mid)
        assert expiry > time.time()*1000
        assert all([await store.r.execute_command('PEXPIRETIME', k)==expiry for k in keys])
    assert await store.delete(ids[0])
    assert not await store.r.exists(store.mem_prefix+ids[0])
    assert not await store.keys_for(ids[0])

@pytest.mark.asyncio
async def test_missing_parent_is_not_recreated_or_returned(store):
    store = build(store)
    await store.ensure(create=True)
    mid = str(uuid.uuid4())
    await store.save(mid, {'text':'original','ttl_days':'1'}, [vector()])
    await store.r.delete(store.mem_prefix+mid)
    assert await store.search(vector(), '', 5)==[]
    assert not await store.r.exists(store.mem_prefix+mid)

@pytest.mark.asyncio
async def test_unmigrated_records_and_changed_budget_fail_closed(store):
    store = build(store)
    await store.r.hset(store.mem_prefix+str(uuid.uuid4()), mapping={'text':'legacy','vector':b'old'})
    with pytest.raises(ValueError, match='migration'):
        await store.ensure(create=True)
    for key in [k async for k in store.r.scan_iter(store.mem_prefix+'*')]:await store.r.delete(key)
    await store.ensure(create=True)
    other = type(store)(store.r, store.mem_prefix, store.base_index, Profile(chunk_tokens=512))
    with pytest.raises(ValueError, match='profile'):
        await other.ensure(create=True)

@pytest.mark.asyncio
async def test_partial_staging_is_invisible_and_permanent_parent_stays_permanent(store):
    store = build(store)
    await store.ensure(create=True)
    mid = str(uuid.uuid4())
    await store.save(mid, {'text':'persistent','ttl_days':'0'}, [vector()])
    keys = await store.keys_for(mid)
    assert await store.r.ttl(keys[0])==-1
    await store.search(vector(), '', 5)
    assert await store.r.ttl(keys[0])==-1
    await store.r.hset(store.vector_prefix+'orphan',mapping={'parent':str(uuid.uuid4()),'generation':'bad','vector':vector(),'tags':''})
    hits = await store.search(vector(), '', 5)
    assert len(hits)==1 and hits[0][0]==mid
