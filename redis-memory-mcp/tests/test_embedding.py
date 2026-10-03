import importlib
import json
from pathlib import Path
import sys
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).parents[1] / 'server'))

@pytest.mark.asyncio
@pytest.mark.parametrize('vectors', [[[1.0]], [[float('nan')]*768], [[0.0]*768], []])
async def test_rejects_invalid_embedding_without_publishing(vectors):
    module = importlib.import_module('embedding')
    def handler(request):
        if request.url.path == '/info':
            return httpx.Response(200, json=module.endpoint_info())
        return httpx.Response(200, content=json.dumps(vectors), headers={'content-type':'application/json'})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    encoder = module.Embedder(module.Profile(), 'http://test', client=client)
    with pytest.raises(ValueError, match='embedding'):
        await encoder.embed(['text'])
    await client.aclose()

@pytest.mark.asyncio
async def test_rejects_wrong_model_before_encoding():
    module = importlib.import_module('embedding')
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={'model_id':'wrong','model_sha':'wrong'})))
    with pytest.raises(ValueError, match='profile'):
        await module.Embedder(module.Profile(), 'http://test', client=client).embed(['text'])
    await client.aclose()

def test_gemma_defaults_and_dimension_configuration(monkeypatch):
    from embedding import Profile
    monkeypatch.delenv('CHUNK_MAX_TOKENS', raising=False)
    monkeypatch.delenv('EMBED_DIMENSION', raising=False)
    assert Profile.from_env().dimension == 256
    assert Profile.from_env().chunk_tokens == 256
    fingerprints = set()
    for dim in [128, 256, 512, 768]:
        monkeypatch.setenv('EMBED_DIMENSION', str(dim))
        p = Profile.from_env()
        assert p.dimension == dim
        fingerprints.add(p.fingerprint)
    assert len(fingerprints) == 4
    monkeypatch.setenv('EMBED_DIMENSION', '384')
    with pytest.raises(ValueError, match='EMBED_DIMENSION'):
        Profile.from_env()

@pytest.mark.asyncio
async def test_gemma_truncates_then_normalizes_full_embedding():
    from embedding import Embedder, Profile, endpoint_info
    import numpy as np
    def handler(request):
        if request.url.path == '/info':return httpx.Response(200,json=endpoint_info())
        return httpx.Response(200,json=[[1.0]*768])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        vectors=await Embedder(Profile(), 'http://test',client=client).embed(['text'])
    v=np.frombuffer(vectors[0],dtype='<f4')
    assert v.shape==(256,) and np.isclose(np.linalg.norm(v),1)
