"""Explicit-role Gemma bridge with portable artifacts outside the search index.

Returns raw first-256 components; native VectorLink owns the final L2 step.
The pinned profile supplies identities, not permission to silently truncate input.
"""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.request
from ..storage import (check_init_stage, directory_lock, initialize_database, init_stage,
                       readonly_database, validate_database, verify_schema)


VECTOR_SCHEMA = (
    'CREATE TABLE vectors (key TEXT PRIMARY KEY NOT NULL, artifact TEXT NOT NULL)',
    'CREATE TABLE vector_integrity (key TEXT PRIMARY KEY NOT NULL, sha256 TEXT NOT NULL)',
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def cache_artifact(db, key, artifact):
    """Retain new inference or verified Git bytes without changing artifact format."""
    value = canonical(artifact)
    digest = hashlib.sha256(value.encode()).hexdigest()
    inserted = db.execute('INSERT OR IGNORE INTO vectors VALUES (?,?)', (key, value))
    if inserted.rowcount:
        db.execute('INSERT INTO vector_integrity VALUES (?,?)', (key, digest))
        return
    old = db.execute('SELECT artifact,sha256 FROM vectors LEFT JOIN vector_integrity USING(key) '
                     'WHERE key=?', (key,)).fetchone()
    if old[0] != value:
        raise ValueError('conflicting retained vector artifacts')
    if old[1] != digest:
        raise ValueError('cached vector integrity missing or mismatched')


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


def verified_vectors(db):
    rows = db.execute('SELECT v.key,v.artifact,i.sha256 FROM vectors v LEFT JOIN vector_integrity i USING(key)')
    for key, value, digest in rows:
        if not isinstance(value, str) or hashlib.sha256(value.encode()).hexdigest() != digest:
            raise ValueError('vector integrity missing or mismatched')
        artifact = json.loads(value)
        if artifact_key(artifact) != key:
            raise ValueError('vector cache key mismatch')
        yield key, artifact


def verify_cache_contents(db):
    verify_schema(db, VECTOR_SCHEMA)
    if db.execute('SELECT key FROM vector_integrity EXCEPT SELECT key FROM vectors LIMIT 1').fetchone():
        raise ValueError('orphaned vector integrity')
    for _ in verified_vectors(db):
        pass


def check_cache(cache, *, locked=False, recover=False):
    cache = Path(cache)
    path = cache / 'vectors.sqlite3'
    if cache.is_symlink():
        raise ValueError('unsupported vector cache directory')
    if not cache.exists():
        return
    if not cache.is_dir():
        raise NotADirectoryError(cache)
    if not path.exists() and not locked:
        with directory_lock(cache):
            return check_cache(cache, locked=True, recover=recover)
    try:
        if not path.exists() and not path.is_symlink():
            check_init_stage(path)
            if any(p != init_stage(path) for p in cache.iterdir()):
                raise ValueError('unsupported vector storage without its database')
            return
        validate_database(path, verify_cache_contents, recover=recover)
    except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as error:
        raise ValueError(f'unsupported vector storage: {error}') from error


def initialize_cache(cache):
    cache = Path(cache)
    check_cache(cache, recover=True)
    path = cache / 'vectors.sqlite3'
    if not path.exists():
        initialize_database(path, VECTOR_SCHEMA, lambda: check_cache(cache, locked=True))


def artifact_key(artifact):
    profile_id = hashlib.sha256(canonical(artifact['profile']).encode()).hexdigest()
    if artifact['profile_id'] != profile_id or artifact['role'] not in ('document', 'query'):
        raise ValueError('vector artifact identity mismatch')
    if hashlib.sha256(artifact['input'].encode()).hexdigest() != artifact['input_sha256']:
        raise ValueError('vector input digest mismatch')
    raw_vectors([artifact['raw768']], 1)
    return hashlib.sha256(canonical([profile_id, artifact['role'], artifact['input']]).encode()).hexdigest()


def merge_verified_cache(source, destination):
    """Merge an explicitly supplied, current verified cache in one transaction."""
    original = Path(source) / 'vectors.sqlite3'
    with readonly_database(original) as source_db:
        verify_cache_contents(source_db)
        if original.parent.resolve() == Path(destination).resolve():
            return
        initialize_cache(destination)
        with closing(sqlite3.connect(Path(destination) / 'vectors.sqlite3')) as target, target:
            for key, artifact in verified_vectors(source_db):
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
                    if artifact_key(artifact) != key:
                        raise ValueError('cached vector identity mismatch')
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
