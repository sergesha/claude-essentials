"""Pinned EmbeddingGemma Q4 profile. A changed input budget is a different index profile."""
from dataclasses import dataclass, asdict
import hashlib
import json
import os

MODEL = 'onnx-community/embeddinggemma-300m-ONNX'
REVISION = '5090578d9565bb06545b4552f76e6bc2c93e4a66'
TOKENIZER_SHA256 = '4dda02faaf32bc91031dc8c88457ac272b00c1016cc679757d1c441b248b9c47'

@dataclass(frozen=True)
class Profile:
    chunk_tokens: int = 256
    model: str = MODEL
    revision: str = REVISION
    tokenizer_sha256: str = TOKENIZER_SHA256
    dimension: int = 256
    pooling: str = 'sentence_embedding'
    quantization: str = 'q4'
    normalize: bool = True
    overlap: int = 32
    max_chunks: int = 256
    format: str = 'gemma-title-quarter-max64-text-newline-code-paragraph-v1'
    index_algorithm: str = 'FLAT'

    @classmethod
    def from_env(cls):
        try:
            budget = int(os.getenv('CHUNK_MAX_TOKENS', '256'))
        except ValueError:
            raise ValueError('CHUNK_MAX_TOKENS must be an integer between 64 and 2048') from None
        if not 64 <= budget <= 2048:
            raise ValueError('CHUNK_MAX_TOKENS must be between 64 and 2048')
        try:
            dimension = int(os.getenv('EMBED_DIMENSION', '256'))
        except ValueError:
            raise ValueError('EMBED_DIMENSION must be 128, 256, 512 or 768') from None
        if dimension not in (128, 256, 512, 768):
            raise ValueError('EMBED_DIMENSION must be 128, 256, 512 or 768')
        return cls(chunk_tokens=budget, dimension=dimension)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.json.encode()).hexdigest()

    @property
    def json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))

class Embedder:
    def __init__(self, profile, url, socket='', *, client=None):
        import httpx
        self.profile = profile
        self.url = url.rstrip('/')
        self.client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket) if socket else None,
            timeout=60.0,
        )
        self._tokenizer = None

    def tokenizer(self):
        if self._tokenizer is None:
            from pathlib import Path
            from tokenizers import Tokenizer
            filename = os.getenv('EMBED_TOKENIZER_PATH')
            if not filename:
                from huggingface_hub import hf_hub_download
                filename = hf_hub_download(MODEL, 'tokenizer.json', revision=REVISION)
            data = Path(filename).read_bytes()
            if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
                raise ValueError('Tokenizer does not match the embedding profile')
            self._tokenizer = Tokenizer.from_str(data.decode())
            self._tokenizer.no_truncation()
            self._tokenizer.no_padding()
        return self._tokenizer

    async def embed(self, texts):
        import numpy as np
        info = await self.client.get(self.url + '/info')
        info.raise_for_status()
        info = info.json()
        if any(info.get(k) != v for k, v in endpoint_info().items()):
            raise ValueError('Endpoint does not match the embedding profile')
        result = []
        batch_size = max(1, min(4, 1024 // self.profile.chunk_tokens))
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start+batch_size]
            response = await self.client.post(self.url + '/embed', json={
                'inputs': batch, 'normalize': True, 'truncate': False,
            })
            response.raise_for_status()
            try:
                vectors = np.asarray(response.json(), dtype=np.float32)
            except (TypeError, ValueError):
                raise ValueError('Invalid embedding response') from None
            if vectors.shape != (len(batch), 768) or not np.isfinite(vectors).all():
                raise ValueError('Invalid embedding dimension, cardinality or values')
            vectors = vectors[:, :self.profile.dimension]
            norms = np.linalg.norm(vectors, axis=1)
            if not np.isfinite(norms).all() or np.any(norms == 0):
                raise ValueError('Invalid embedding norm')
            vectors = vectors / norms[:, None]
            result.extend(v.tobytes() for v in vectors)
        return result

    async def record(self, text, label='', code=''):
        import asyncio
        from chunking import split
        tokenizer = await asyncio.to_thread(self.tokenizer)
        source = text + '\n' + code if code else text
        chunks = split(source, label, tokenizer, self.profile.chunk_tokens, max_chunks=self.profile.max_chunks)
        return await self.embed([chunk.input for chunk in chunks])

    async def query(self, text):
        import asyncio
        tokenizer = await asyncio.to_thread(self.tokenizer)
        text = 'task: search result | query: ' + text
        if len(tokenizer.encode(text).ids) > 2048:
            raise ValueError('Search query exceeds 2048 tokens; shorten the query')
        return (await self.embed([text]))[0]

    async def aclose(self):
        await self.client.aclose()


def endpoint_info():
    return {'model_id': MODEL, 'model_sha': REVISION, 'quantization': 'q4',
            'output': 'sentence_embedding', 'dimension': 768, 'max_input_tokens': 2048,
            'tokenizer_sha256': TOKENIZER_SHA256}
