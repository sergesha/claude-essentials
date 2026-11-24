"""Explicit-role Gemma bridge with portable artifacts outside the search index.

Returns raw first-256 components; native VectorLink owns the final L2 step.
The pinned profile supplies identities, not permission to silently truncate input.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.request


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def cache_artifact(db, key, artifact):
    """Retain new inference or verified Git bytes without changing artifact format."""
    value = canonical(artifact)
    old = db.execute('SELECT artifact FROM vectors WHERE key=?', (key,)).fetchone()
    if old and old[0] != value:
        raise ValueError('conflicting retained vector artifacts')
    digest = hashlib.sha256(value.encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO vectors VALUES (?,?)', (key, value))
    db.execute('INSERT OR REPLACE INTO vector_integrity VALUES (?,?)', (key, digest))


def raw_vectors(response, count):
    if not isinstance(response, list) or len(response) != count:
        raise ValueError('embedding response cardinality mismatch')
    for vector in response:
        if not isinstance(vector, list) or len(vector) != 768:
            raise ValueError('embedding response must contain 768D vectors')
        if not all(type(x) in (int, float) and math.isfinite(x) for x in vector):
            raise ValueError('embedding response contains non-finite or nonnumeric values')
        if sum(x*x for x in vector[:256]) <= 0:
            raise ValueError('embedding prefix cannot be normalized')
    return response


def initialize_cache(cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache / 'vectors.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, artifact TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS vector_integrity (key TEXT PRIMARY KEY, sha256 TEXT NOT NULL)')
        db.execute('SELECT key, artifact FROM vectors LIMIT 0')
        db.execute('SELECT key, sha256 FROM vector_integrity LIMIT 0')


def artifact_key(artifact):
    profile_id = hashlib.sha256(canonical(artifact['profile']).encode()).hexdigest()
    if artifact['profile_id'] != profile_id or artifact['role'] not in ('document', 'query'):
        raise ValueError('vector artifact identity mismatch')
    if hashlib.sha256(artifact['input'].encode()).hexdigest() != artifact['input_sha256']:
        raise ValueError('vector input digest mismatch')
    raw_vectors([artifact['raw768']], 1)
    return hashlib.sha256(canonical([profile_id, artifact['role'], artifact['input']]).encode()).hexdigest()


def merge_verified_cache(source, destination):
    """Read a retained cache in place. Publish verified rows together or roll back."""
    original = Path(source) / 'vectors.sqlite3'
    if not original.exists():
        return
    initialize_cache(destination)
    with sqlite3.connect(Path(destination) / 'vectors.sqlite3') as target:
        copy_verified_vectors(original, target)


def copy_verified_vectors(original, target):
    """Insert source rows into the caller's transaction without committing it."""
    with sqlite3.connect(original.as_uri() + '?mode=ro', uri=True) as old:
        if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vector_integrity'").fetchone():
            # Older caches lack checksums. Their canonical document vectors come from Git.
            return
        rows = old.execute('SELECT v.key,v.artifact,i.sha256 FROM vectors v LEFT JOIN vector_integrity i USING(key)')
        for key, value, digest in rows:
            if digest is None:
                continue  # Unverified legacy row in a partially upgraded cache.
            if hashlib.sha256(value.encode()).hexdigest() != digest:
                raise ValueError('legacy vector integrity mismatch')
            artifact = json.loads(value)
            if artifact_key(artifact) != key:
                raise ValueError('legacy vector cache key mismatch')
            cache_artifact(target, key, artifact)


class EmbeddingProvider:
    def __init__(self, profile_path, cache, endpoint):
        self.profile = json.loads(Path(profile_path).read_text())
        digest = hashlib.sha256(canonical(self.profile['profile']).encode()).hexdigest()
        if digest != self.profile['profile_id']:
            raise ValueError('embedding profile digest mismatch')
        self.endpoint = endpoint.rstrip('/')
        self.cache = Path(cache)
        initialize_cache(self.cache)
        self.stats = {'inference_calls': 0, 'cache_hits': 0, 'inference_seconds': 0.0}

    def connect(self):
        return sqlite3.connect(self.cache / 'vectors.sqlite3')

    def request(self, path, body=None):
        req = urllib.request.Request(self.endpoint + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)

    def embed(self, body):
        role = body.get('role')
        if role not in ('document', 'query'):
            raise ValueError('explicit document/query role required')
        if body.get('model') != self.profile['model_namespace'] or body.get('dimensions') != 256:
            raise ValueError('embedding profile mismatch')
        texts = body.get('input')
        if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
            raise ValueError('nonempty text input array required')
        prefix = 'title: none | text: ' if role == 'document' else 'task: search result | query: '
        inputs = [prefix + text for text in texts]
        keys = [hashlib.sha256(canonical([self.profile['profile_id'], role, text]).encode()).hexdigest() for text in inputs]
        artifacts = {}
        with self.connect() as db:
            for key in set(keys):
                row = db.execute('SELECT artifact FROM vectors WHERE key=?', (key,)).fetchone()
                if row:
                    integrity = db.execute('SELECT sha256 FROM vector_integrity WHERE key=?', (key,)).fetchone()
                    if not integrity or integrity[0] != hashlib.sha256(row[0].encode()).hexdigest():
                        raise ValueError('cached vector integrity missing or mismatched; restore retained vectors from a verified atlas archive')
                    artifact = json.loads(row[0])
                    raw_vectors([artifact['raw768']], 1)
                    expected = [self.profile['profile_id'], role, inputs[keys.index(key)]]
                    if [artifact['profile_id'], artifact['role'], artifact['input']] != expected:
                        raise ValueError('cached vector identity mismatch')
                    artifacts[key] = artifact
            self.stats['cache_hits'] += sum(k in artifacts for k in keys)
            missing = list(dict.fromkeys(k for k in keys if k not in artifacts))
            if missing:
                info = self.request('/info')
                if any(info.get(k) != v for k, v in self.profile['profile']['endpoint'].items()):
                    raise ValueError('live Gemma endpoint does not match pinned profile')
                final_inputs = [inputs[keys.index(k)] for k in missing]
                started = time.monotonic()
                self.stats['inference_calls'] += 1
                vectors = raw_vectors(self.request('/embed', {'inputs': final_inputs, 'truncate': False}), len(missing))
                self.stats['inference_seconds'] += time.monotonic() - started
                for key, text, vector in zip(missing, final_inputs, vectors):
                    artifact = {'profile_id': self.profile['profile_id'], 'profile': self.profile['profile'],
                                'role': role, 'input': text,
                                'input_sha256': hashlib.sha256(text.encode()).hexdigest(), 'raw768': vector}
                    cache_artifact(db, key, artifact)
                    artifacts[key] = artifact
        return {'model': self.profile['model_namespace'],
                'data': [{'index': i, 'embedding': artifacts[key]['raw768'][:256]} for i, key in enumerate(keys)]}
