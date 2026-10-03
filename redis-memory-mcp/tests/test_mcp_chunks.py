import importlib
from pathlib import Path
import sys,struct,uuid,os
import pytest
import redis.asyncio as redis
sys.path.insert(0,str(Path(__file__).parents[1]/'server'))

@pytest.mark.asyncio
async def test_mcp_saves_chunked_parent_lists_and_deletes(monkeypatch):
    url=os.getenv('REDIS_GRANITE_TEST_URL')
    if not url:pytest.skip('isolated Redis required')
    server=importlib.import_module('memory_mcp')
    prefix='ns:integration_'+uuid.uuid4().hex+':'
    monkeypatch.setattr(server,'_scope',lambda *args:(prefix+'mem:',prefix+'kv:',prefix+'idx'))
    monkeypatch.setattr(server,'_redis',lambda:redis.from_url(url))
    class Encoder:
        def __init__(self,*a,**kw):pass
        async def record(self,*a):return [struct.pack('<256f',1,*([0]*255))]*3
        async def query(self,*a):return struct.pack('<256f',1,*([0]*255))
        async def aclose(self):pass
    monkeypatch.setattr(server,'Embedder',Encoder,raising=False)
    async def old_embed(text):return [1.0]+[0.0]*767
    monkeypatch.setattr(server,'_embed',old_embed)
    r=redis.from_url(url)
    try:
        saved=await server.mem_save('complete record',code='print(1)',tags='fixture')
        mid=saved.split('mem[')[1].split(']')[0]
        parent=await r.hgetall(prefix+'mem:'+mid)
        assert b'vector' not in parent
        assert any(k.startswith(b'_chunks_') and v==b'3' for k,v in parent.items())
        found=await server.mem_search('record',tags='fixture')
        assert found.count('ID:'+mid)==1 and 'print(1)' in found
        assert mid in await server.mem_list(tag='fixture')
        assert 'Deleted' in await server.mem_delete(mid)
        assert 'No memories' in await server.mem_search('record')
    finally:
        for index in await r.execute_command('FT._LIST'):
            if index.decode().startswith(prefix):await r.execute_command('FT.DROPINDEX',index)
        keys=[k async for k in r.scan_iter(prefix+'*')]
        if keys:await r.delete(*keys)
        await r.aclose()
