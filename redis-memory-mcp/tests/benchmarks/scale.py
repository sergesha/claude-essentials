"""Storage/search scaling using cached real Gemma vectors, not embedding throughput."""
import asyncio,json,os,sys,time,uuid
from pathlib import Path
import numpy as np
import redis.asyncio as redis
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'server'))
from embedding import Profile,Embedder
from memory_index import MemoryIndex
async def main():
 r=redis.from_url(os.environ['REDIS_BENCH_URL'],protocol=2)
 if await r.dbsize():raise ValueError('Use an empty isolated Redis')
 e=Embedder(Profile(),os.environ['EMBED_BENCH_URL']);results=[]
 try:
  vectors=await e.record('Synthetic storage fixture. The searchable fact is retained in the full canonical record.','Storage fixture')
  query=await e.query('searchable fact')
  for repeat in [1,2]:
   root='ns:scale_'+uuid.uuid4().hex+':';s=MemoryIndex(r,root+'mem:',root+'idx',Profile());await s.ensure(create=True)
   start=time.perf_counter()
   for i in range(10000):
    await s.save(str(uuid.uuid4()),{'text':'Synthetic storage fixture '+str(i),'label':'Storage fixture','tags':'fixture','ttl_days':'1','timestamp':'1'},vectors)
    if i+1 not in [1000,10000]:continue
    elapsed=time.perf_counter()-start;info=await r.info('memory');stats=await r.info('stats');lat={}
    for concurrency in [1,4]:
     semaphore=asyncio.Semaphore(concurrency)
     async def search():
      async with semaphore:
       at=time.perf_counter();hits=await s.search(query,'fixture',5)
       assert len(hits)==len({h[0] for h in hits})==5
       assert all(h[1]['text'].startswith('Synthetic storage fixture ') for h in hits)
       return (time.perf_counter()-at)*1000
     samples=await asyncio.gather(*(search() for _ in range(104)))
     lat[str(concurrency)]={'p50_ms':float(np.percentile(samples,50)),'p95_ms':float(np.percentile(samples,95)),'samples':samples}
    row={'repeat':repeat,'parents':i+1,'chunks_per_parent':len(vectors),'cumulative_save_seconds':elapsed,'used_bytes':info['used_memory'],'evictions':stats['evicted_keys'],'cached_query_latency':lat};results.append(row);print(json.dumps({k:v for k,v in row.items() if k!='cached_query_latency'}),flush=True)
   await r.execute_command('FT.DROPINDEX',s.index)
   keys=[k async for k in r.scan_iter(root+'*')]
   for i in range(0,len(keys),500):await r.delete(*keys[i:i+500])
  Path(os.environ.get('BENCH_OUTPUT','/tmp/gemma-scale.json')).write_text(json.dumps(results,indent=2))
 finally:await e.aclose();await r.aclose()
if __name__=='__main__':asyncio.run(main())
