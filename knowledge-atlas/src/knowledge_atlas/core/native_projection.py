"""Materialize accepted Git content in one native database, retaining old commits."""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import time
from .git_store import git
from .graph_stage import records_for, record_schema
from .vector_projection import chunk_schema
from .vector_chunks import chunks_for
from .embedding_provider import initialize_cache, raw_vectors


def canonical_record(record):
    result = dict(record)
    result['@id'] = result['@id'].split('/data/')[-1]
    result['links'] = sorted(v.split('/data/')[-1] for v in result.get('links', []))
    for field in ('subject', 'object'):
        if field in result:
            result[field] = result[field].split('/data/')[-1]
    for field in ('recorded_at', 'accepted_at'):
        if field in result:
            result[field] = datetime.fromisoformat(result[field].replace('Z', '+00:00')).isoformat()
    return result


class NativeProjection:
    def __init__(self, store, graph, tokenizer, formatter, cache):
        self.store, self.graph = store, graph
        self.tokenizer, self.formatter, self.cache = tokenizer, formatter, Path(cache)

    def build(self, content):
        started = time.monotonic()
        snapshot = self.store.read(content)
        if snapshot['manifest']['status'] != 'accepted':
            raise ValueError('accepted Git content required')
        profile = json.loads(git(self.store.root, 'show', content + ':embedding-profile.json'))
        records = records_for(snapshot, content)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            path.write_text(json.dumps(profile))
            chunks = chunks_for(records, path, self.tokenizer, self.formatter)
        initialize_cache(self.cache)
        g = self.graph
        try:
            g.call('GET', '/api/document/' + g.database + '?graph_type=schema&as_list=true')
        except RuntimeError:
            if not g.trace or g.trace[-1]['status'] != 404:
                raise
            g.call('POST', '/api/db/' + g.database, {'label': 'PoC9 reviewed atlas projections', 'schema': True})
            g.call('POST', '/api/document/' + g.database + '?graph_type=schema&author=poc9&message=schema',
                   [record_schema(embed=False), chunk_schema()])
        # Full document replacement in the SAME versioned database; this is a
        # bounded baseline, not a claim of optimal incremental update cost.
        result = g.call('POST', '/api/document/' + g.database + '?full_replace=true&author=poc9&message=reviewed-content', records + chunks)
        commit = result['headers']['terminusdb-data-version'].split(':')[-1]
        materialized = time.monotonic()
        g.call('POST', '/api/index/' + g.database, {})
        deadline = time.monotonic() + 60
        while True:
            index = g.call('GET', '/api/index/' + g.database)['body']
            if index.get('last_indexed_commit') == commit:
                break
            if index.get('error') or time.monotonic() > deadline:
                raise RuntimeError('exact index is not ready: ' + json.dumps(index))
            time.sleep(.25)
        indexed = time.monotonic()
        actual = g.documents(commit)
        actual_records = {d['key']: canonical_record(d) for d in actual if d['@type'] == 'Record'}
        if actual_records != {d['key']: canonical_record(d) for d in records}:
            raise ValueError('graph records do not match reviewed Git content')
        actual_chunks = {d['key']: d for d in actual if d['@type'] == 'Chunk'}
        if set(actual_chunks) != {d['key'] for d in chunks}:
            raise ValueError('graph chunk membership mismatch')
        for d in chunks:
            saved = actual_chunks[d['key']]
            if any(saved.get(k) != v for k,v in d.items() if k != 'record') or saved['record'].split('/data/')[-1] != d['record']:
                raise ValueError('graph chunk contents mismatch')
        export = g.call('GET', '/api/plugin/search-embeddings/' + g.database + '/local/commit/' + commit + '?compress_ids=false')['body']
        if export.get('served_commit') != commit:
            raise ValueError('export served commit mismatch')
        vectors = export['doc_embeddings']
        expected_ids = {'terminusdb:///data/Chunk/' + d['key'] for d in chunks}
        if not expected_ids.issubset(vectors):
            raise ValueError('native vectors missing for current graph chunks')
        # Native changed-text updates can retain deleted chunk rows. Canonical
        # membership is the exact graph, also used BEFORE ranking by the reader.
        # Retain this limitation explicitly; do not relabel native export as exact.
        excluded_ids = sorted(set(vectors) - expected_ids)
        with sqlite3.connect(self.cache / 'vectors.sqlite3') as db:
            cached = [json.loads(row[0]) for row in db.execute('SELECT artifact FROM vectors')]
        artifacts = {};max_error = 0.0
        for d in chunks:
            final_input = 'title: none | text: ' + d['text']
            artifact = next((a for a in cached if a['profile_id'] == profile['profile_id']
                             and a['role'] == 'document' and a['input'] == final_input), None)
            if artifact is None:
                raise ValueError('portable vector artifact missing')
            raw = raw_vectors([artifact['raw768']], 1)[0][:256]
            expected_norm = math.sqrt(sum(x*x for x in raw))
            vector = vectors['terminusdb:///data/Chunk/' + d['key']]
            if len(vector) != 256 or not all(math.isfinite(x) for x in vector):
                raise ValueError('native vector shape mismatch')
            error = max(abs(x/expected_norm-y) for x,y in zip(raw, vector))
            if error > 1e-6:
                raise ValueError('native vector differs from profile artifact')
            max_error = max(max_error, error)
            artifacts[hashlib.sha256(final_input.encode()).hexdigest()] = artifact
        return {'content_commit': content, 'database': g.database, 'graph_commit': commit,
                'search_checkpoint': commit, 'ready': True, 'profile_id': profile['profile_id'],
                'record_count': len(records), 'chunk_count': len(chunks),
                'vector_artifacts': artifacts, 'vector_max_error': max_error,
                'native_export_excluded_ids': excluded_ids,
                'vector_export_scope': 'exact graph chunk membership',
                'timing': {'materialize_seconds': materialized-started, 'index_seconds': indexed-materialized,
                           'verify_seconds': time.monotonic()-indexed}, 'update_mode': 'same-database-full-document-replacement'}
