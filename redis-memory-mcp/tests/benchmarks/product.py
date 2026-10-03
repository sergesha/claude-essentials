"""Explicit isolated benchmark. Never point REDIS_BENCH_URL at production."""
import asyncio,hashlib,json,os,sys,time,uuid
from pathlib import Path
import numpy as np
import redis.asyncio as redis
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'server'))
from embedding import Profile,Embedder
from memory_index import MemoryIndex

async def main():
    url=os.environ['REDIS_BENCH_URL'];endpoint=os.environ['EMBED_BENCH_URL']
    r=redis.from_url(url,protocol=2)
    if await r.dbsize():raise ValueError('Benchmark requires an EMPTY dedicated Redis database')
    if (await r.config_get('maxmemory-policy'))['maxmemory-policy']!='noeviction':raise ValueError('Require noeviction')
    corpus=json.loads((Path(__file__).parent/'corpus.json').read_text())
    profile=Profile.from_env();encoder=Embedder(profile,endpoint)
    store=MemoryIndex(r,'ns:product_benchmark:mem:','idx:product_benchmark',profile)
    mapping={};dv=[];owners=[];start=time.perf_counter()
    try:
        await store.ensure(create=True)
        for d in corpus['documents']:
            mid=str(uuid.uuid5(uuid.NAMESPACE_URL,d['id']));mapping[mid]=d['id']
            vectors=await encoder.record(d['text'],d['label'])
            dv.extend(np.frombuffer(v,dtype='<f4') for v in vectors);owners.extend([mid]*len(vectors))
            await store.save(mid,{'text':d['text'],'label':d['label'],'tags':'benchmark','timestamp':'1','ttl_days':'1'},vectors)
        ingestion=time.perf_counter()-start;rows=[];dv=np.asarray(dv)
        for q in corpus['queries']:
            vector=await encoder.query(q['text']);v=np.frombuffer(vector,dtype='<f4')
            order=list(dict.fromkeys(owners[j] for j in np.argsort(-(dv@v),kind='stable')))
            rank=[mapping[m] for m in order].index(q['relevant'][0])+1
            hits=await store.search(vector,'benchmark',5)
            assert [h[0] for h in hits]==order[:5]
            assert all(h[1]['text']==next(d['text'] for d in corpus['documents'] if d['id']==mapping[h[0]]) for h in hits)
            rows.append({'id':q['id'],'rank':rank})
        latencies={}
        for concurrency in [1,4]:
            sem=asyncio.Semaphore(concurrency)
            async def search(q):
                async with sem:
                    start=time.perf_counter();await store.search(await encoder.query(q['text']),'benchmark',5)
                    return (time.perf_counter()-start)*1000
            times=await asyncio.gather(*(search(q) for _ in range(4) for q in corpus['queries']))
            latencies[str(concurrency)]={'p50_ms':float(np.percentile(times,50)),'p95_ms':float(np.percentile(times,95)),'samples':times}
        info=await r.info('memory')
        result={'profile':json.loads(profile.json),'corpus_sha256':hashlib.sha256((Path(__file__).parent/'corpus.json').read_bytes()).hexdigest(),'vectors':len(dv),'ingestion_seconds':ingestion,'top1':sum(x['rank']==1 for x in rows),'top5':sum(x['rank']<=5 for x in rows),'quality':rows,'latencies':latencies,'redis_used_bytes':info['used_memory']}
        Path(os.environ.get('BENCH_OUTPUT','/tmp/gemma-product.json')).write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k not in ('quality','latencies','profile')}))
    finally:
        await encoder.aclose();await r.aclose()

if __name__=='__main__':asyncio.run(main())
