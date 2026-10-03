"""Area-local derived vectors. Parents are canonical; staged vectors are invisible.

Exact FLAT search avoids approximate-index recall loss. Publication, touch and
invalidation check the active generation inside bounded Redis scripts.
"""
import hashlib
import json
import re
import time
import uuid
from redis.exceptions import ResponseError

FIELDS = ('text', 'label', 'code', 'tags', 'timestamp', 'ttl_days')
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')

def decode(v):
    return v.decode() if isinstance(v, bytes) else str(v)

def mapping(raw):
    if isinstance(raw, dict):
        return {decode(k): v for k, v in raw.items()}
    return {decode(raw[i]): raw[i+1] for i in range(0, len(raw), 2)}

def identity(data):
    data = {decode(k): v if isinstance(v, bytes) else str(v).encode() for k, v in data.items()}
    source = b''
    for field in FIELDS:
        value = data.get(field)
        source += b'-;' if value is None else str(len(value)).encode()+b':'+value+b';'
    return hashlib.sha1(source).hexdigest()

_COMMON = """
local function identity(key)
  local s = ''
  for _, f in ipairs({'text','label','code','tags','timestamp','ttl_days'}) do
    local v = redis.call('HGET', key, f)
    if v then s = s .. string.len(v) .. ':' .. v .. ';' else s = s .. '-;' end
  end
  return redis.sha1hex(s)
end
local function expire(key, expiry)
  if expiry == -1 then redis.call('PERSIST', key) else redis.call('PEXPIREAT', key, expiry) end
end
local function now()
  local t = redis.call('TIME'); return t[1]*1000 + math.floor(t[2]/1000)
end
"""
_SNAPSHOT = _COMMON + """
if redis.call('EXISTS', KEYS[1]) == 0 then return {} end
return {redis.call('HGETALL', KEYS[1]), redis.call('PEXPIRETIME', KEYS[1]), identity(KEYS[1])}
"""
_PUBLISH = _COMMON + """
local state, parent = KEYS[1], KEYS[2]
local profile, generation, expiry, expected, oldexpiry, gf, cf, owner = unpack(ARGV,1,8)
expiry = tonumber(expiry); oldexpiry = tonumber(oldexpiry)
if owner == '' then
  if redis.call('HGET',state,'status') ~= 'active' or redis.call('HGET',state,'profile') ~= profile then
    return redis.error_reply('Embedding profile changed; migration required')
  end
else
  if redis.call('HGET',state,'owner') ~= owner or redis.call('HGET',state,'target') ~= profile then
    return redis.error_reply('Migration ownership changed')
  end
  if redis.call('GET',KEYS[3]) ~= owner then return redis.error_reply('Migration lock lost') end
end
local exists = redis.call('EXISTS',parent)
if expected == '' then
  if exists ~= 0 then return redis.error_reply('Memory already exists') end
else
  if exists == 0 then return 0 end
  if identity(parent) ~= expected or redis.call('PEXPIRETIME',parent) ~= oldexpiry then
    return redis.error_reply('Source changed during embedding')
  end
end
if expiry ~= -1 and expiry <= now() then return 0 end
local first = 4
local n = #KEYS-first+1
if n < 1 or n > 256 then return redis.error_reply('Invalid chunk count') end
for i=first,#KEYS do
  if redis.call('TYPE',KEYS[i]).ok ~= 'hash' or redis.call('HGET',KEYS[i],'generation') ~= generation
    or redis.call('HGET',KEYS[i],'profile') ~= profile then
    return redis.error_reply('Incomplete chunk generation')
  end
  local vector = redis.call('HGET',KEYS[i],'vector')
  if not vector or string.len(vector) ~= tonumber(ARGV[10]) then return redis.error_reply('Invalid chunk vector') end
end
-- Everything that can be validated is checked before the visibility switch.
-- Staging hashes can survive errors, but a parent never references a partial set.
local values = cjson.decode(ARGV[9])
local args = {}
for k,v in pairs(values) do table.insert(args,k);table.insert(args,v) end
if #args > 0 then redis.call('HSET',parent,unpack(args)) end
for i=first,#KEYS do expire(KEYS[i],expiry) end
redis.call('HSET',parent,gf,generation,cf,n)
expire(parent,expiry)
return 1
"""
_TOUCH = _COMMON + """
local state,parent = KEYS[1],KEYS[2]
if redis.call('HGET',state,'status') ~= 'active' or redis.call('HGET',state,'profile') ~= ARGV[1] then
  return redis.error_reply('Embedding profile changed; migration required')
end
if redis.call('EXISTS',parent)==0 or redis.call('HGET',parent,ARGV[2]) ~= ARGV[3] then return {} end
local count=tonumber(redis.call('HGET',parent,ARGV[4]) or '0')
if count<1 or count>256 then return redis.error_reply('Invalid parent generation') end
local days=tonumber(redis.call('HGET',parent,'ttl_days') or '90')
local expiry=-1
if days>0 then expiry=now()+days*86400000 end
expire(parent,expiry)
for i=0,count-1 do
  local key=ARGV[5]..ARGV[3]..':'..i
  if redis.call('EXISTS',key)==1 and redis.call('HGET',key,'generation')==ARGV[3] then expire(key,expiry) end
end
return {redis.call('HGETALL',parent),redis.call('TTL',parent)}
"""
_DELETE = """
if redis.call('HGET',KEYS[1],'status') ~= 'active' or redis.call('HGET',KEYS[1],'profile') ~= ARGV[1] then
  return redis.error_reply('Embedding profile changed; migration required')
end
if redis.call('EXISTS',KEYS[2])==0 then return 0 end
local generation=redis.call('HGET',KEYS[2],ARGV[2])
local count=tonumber(redis.call('HGET',KEYS[2],ARGV[3]) or '0')
if count<0 or count>256 then return redis.error_reply('Invalid chunk count') end
redis.call('DEL',KEYS[2])
if generation then for i=0,count-1 do redis.call('DEL',ARGV[4]..generation..':'..i) end end
return 1
"""

class MemoryIndex:
    def __init__(self, r, mem_prefix, base_index, profile):
        if not mem_prefix.endswith('mem:'):
            raise ValueError('Invalid memory area')
        self.r, self.mem_prefix, self.base_index, self.profile = r, mem_prefix, base_index, profile
        root = mem_prefix[:-4]+'emb:'
        self.state = root+'state'
        self.vector_prefix = root+profile.fingerprint+':'
        self.index = base_index+':emb:'+profile.fingerprint
        self.generation_field = '_emb_'+profile.fingerprint
        self.count_field = '_chunks_'+profile.fingerprint

    async def ensure_index(self):
        try:
            info = await self.r.execute_command('FT.INFO',self.index)
        except ResponseError as exc:
            if 'unknown index name' not in str(exc).lower():raise
            try:
                await self.r.execute_command('FT.CREATE',self.index,'ON','HASH','PREFIX',1,self.vector_prefix,'SCHEMA',
                    'parent','TAG','generation','TAG','profile','TAG','tags','TAG','SEPARATOR',',',
                    'vector','VECTOR','FLAT',6,'TYPE','FLOAT32','DIM',self.profile.dimension,'DISTANCE_METRIC','COSINE')
            except ResponseError as create_exc:
                if 'index already exists' not in str(create_exc).lower():raise
            info = await self.r.execute_command('FT.INFO',self.index)
        info = mapping(info)
        definition = mapping(info['index_definition'])
        attributes = [mapping(a) for a in info['attributes']]
        vector = next((a for a in attributes if decode(a.get('attribute',''))=='vector'),{})
        for name in ('parent', 'generation', 'profile', 'tags'):
            field = next((a for a in attributes if decode(a.get('attribute','')) == name), {})
            if decode(field.get('type', '')) != 'TAG' or (name == 'tags' and decode(field.get('SEPARATOR', field.get('separator', ','))) != ','):
                raise ValueError('Index filter schema does not match the selected profile')
        if (decode(definition.get('key_type',''))!='HASH' or [decode(x) for x in definition.get('prefixes',[])]!=[self.vector_prefix]
            or decode(vector.get('algorithm',''))!='FLAT' or int(vector.get('dim',0))!=self.profile.dimension
            or decode(vector.get('data_type',''))!='FLOAT32' or decode(vector.get('distance_metric',''))!='COSINE'):
            raise ValueError('Index does not match the selected embedding profile/area')

    async def ensure(self, *, create=False):
        state = await self.r.hgetall(self.state)
        if not state:
            async for key in self.r.scan_iter(self.mem_prefix+'*',count=200):
                if await self.r.hexists(key,'text'):
                    raise ValueError('Existing memories require an explicit embedding migration')
            if not create:return False
            await self.ensure_index()
            await self.r.eval("if redis.call('EXISTS',KEYS[1])==0 then redis.call('HSET',KEYS[1],'status','active','profile',ARGV[1],'config',ARGV[2]) end return 1",1,self.state,self.profile.fingerprint,self.profile.json)
            state = await self.r.hgetall(self.state)
        state = mapping(state)
        if decode(state.get('status',''))!='active' or decode(state.get('profile',''))!=self.profile.fingerprint:
            raise ValueError('Area embedding profile differs or migration is incomplete; run migration')
        await self.ensure_index()
        return True

    async def snapshot(self, mid):
        self._id(mid)
        raw = await self.r.eval(_SNAPSHOT,1,self.mem_prefix+mid)
        if not raw:return None
        return mapping(raw[0]),int(raw[1]),decode(raw[2])

    def _id(self, mid):
        if not UUID.fullmatch(mid):raise ValueError('Invalid canonical memory ID')

    async def keys_for(self, mid):
        self._id(mid)
        data = await self.r.hmget(self.mem_prefix+mid,self.generation_field,self.count_field)
        if not data[0]:return []
        count = int(data[1] or 0)
        if not 0<count<=256:raise ValueError('Invalid chunk count')
        return [self.vector_prefix+mid+':'+decode(data[0])+':'+str(i) for i in range(count)]

    async def save(self, mid, fields, vectors, *, snapshot=None, owner='', lock=None):
        self._id(mid)
        if not 0<len(vectors)<=self.profile.max_chunks or any(len(v)!=4*self.profile.dimension for v in vectors):
            raise ValueError('Invalid embedding chunks')
        fields = {decode(k):decode(v) for k,v in fields.items() if decode(k) in FIELDS}
        days = int(fields.get('ttl_days','90'))
        if days<0:raise ValueError('ttl_days cannot be negative')
        if snapshot:
            original,expiry,expected=snapshot
        else:
            expiry = int(time.time()*1000)+days*86400000 if days else -1
            expected = ''
        old_keys = await self.keys_for(mid)
        generation = uuid.uuid4().hex
        keys=[self.vector_prefix+mid+':'+generation+':'+str(i) for i in range(len(vectors))]
        # Retry only unpublished generations: callers verify/reuse a published
        # generation before calling save again during an administrative build.
        staged_expiry=min(expiry,int(time.time()*1000)+3600000) if expiry>0 else int(time.time()*1000)+3600000
        pipe=self.r.pipeline(transaction=False)
        for key,vector in zip(keys,vectors):
            pipe.hset(key,mapping={'parent':mid,'generation':generation,'profile':self.profile.fingerprint,'tags':fields.get('tags',''),'vector':vector})
            pipe.pexpireat(key,staged_expiry)
        await pipe.execute()
        result=await self.r.eval(_PUBLISH,3+len(keys),self.state,self.mem_prefix+mid,lock or self.state,*keys,
            self.profile.fingerprint,generation,expiry,expected,expiry if snapshot else -2,
            self.generation_field,self.count_field,owner,json.dumps(fields if not snapshot else {}),4*self.profile.dimension)
        if result and old_keys:
            await self.r.delete(*old_keys)
        return bool(result)

    async def delete(self, mid):
        self._id(mid)
        return bool(await self.r.eval(_DELETE,2,self.state,self.mem_prefix+mid,
            self.profile.fingerprint,self.generation_field,self.count_field,self.vector_prefix+mid+':'))

    async def search(self, vector, tags, top_k):
        if not 1<=top_k<=100:raise ValueError('top_k must be between 1 and 100')
        if not await self.ensure():return []
        info=mapping(await self.r.execute_command('FT.INFO',self.index));total=int(info['num_docs'])
        if not total:return []
        limit=min(total,max(20,top_k*4));result=[];seen=set()
        while True:
            clause=f'(@tags:{{{tags}}})' if tags else '*'
            raw=await self.r.execute_command('FT.SEARCH',self.index,f'{clause}=>[KNN {limit} @vector $v AS score]',
                'PARAMS',2,'v',vector,'SORTBY','score','RETURN',3,'parent','generation','score','LIMIT',0,limit,'DIALECT',2)
            rows=[]
            if isinstance(raw,dict):
                for d in raw.get(b'results',raw.get('results',[])):
                    rows.append((decode(d.get(b'id',d.get('id'))),mapping(d.get(b'extra_attributes',d.get('extra_attributes',{})))))
            else:
                rows=[(decode(raw[i]),mapping(raw[i+1])) for i in range(1,len(raw),2)]

            for key,fields in rows:
                if not key.startswith(self.vector_prefix):raise ValueError('Search hit outside selected area')
                mid=decode(fields.get('parent',''));generation=decode(fields.get('generation',''))
                if not UUID.fullmatch(mid) or mid in seen:continue
                data=await self.r.eval(_TOUCH,2,self.state,self.mem_prefix+mid,self.profile.fingerprint,
                    self.generation_field,generation,self.count_field,self.vector_prefix+mid+':')
                if not data:continue
                seen.add(mid)
                parent_fields={k:decode(v) for k,v in mapping(data[0]).items() if k in FIELDS}
                result.append((mid,parent_fields,float(fields['score']),int(data[1])))
                if len(result)==top_k:break
            if len(result)==top_k or len(rows)<limit or limit==total:break
            limit=min(total,limit*2)
        return result
