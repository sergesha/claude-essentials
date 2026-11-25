"""Retained PoC9 exact reuse/update regressions; real native parser, fixture models."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import pytest
from knowledge_atlas.core.git_store import GitStore
from test_archives import portable as portable

requires_native_utopia = pytest.mark.skipif(not os.environ.get('POC9_EXTRACT_BIN'), reason='explicit native Utopia binary required')

@requires_native_utopia
def test_recorded_incremental_fence_matches_native_parser():
    from knowledge_atlas.core.extract_stage import extract_wiki

    capture = json.loads((Path(__file__).parent/'fixtures/evidence/opus-incremental-fence.json').read_text())
    page = '/wiki/entities/luma.md'
    old, new = {page: 'Original text'}, {page: 'Updated text'}
    previous = {'s': capture['previous_statements'], 'e': [], 'n': [], 't': []}
    profile = {'model': 'offline-replay'}
    baseline = extract_wiki(old, lambda _: '```json\n' + json.dumps(previous) + '\n```', model_profile=profile)
    def replay(response):
        return extract_wiki(new, lambda _: response, previous=baseline, previous_wiki=old, model_profile=profile)

    result = replay(capture['raw_response'])
    assert result['model_calls'][0]['mode'] == 'statement_refs'
    assert len(result['statements']) == 9 and result['skipped'] == 0 and not result['truncated']
    with pytest.raises(RuntimeError, match='extraction failed'):
        replay(capture['raw_response'] + '\ntrailing prose')


@requires_native_utopia
class EfficientIngestTests(unittest.TestCase):
    def test_selected_git_page_drives_codec_candidate_and_bad_reply_stops_snapshot(self):
        import copy
        from knowledge_atlas.core.ingest import ingest
        root = Path(__file__).resolve().parent/'fixtures'
        folder = root / 'evidence/native-components/row-reference-probe/run-statements-only'
        fixture = json.loads((folder / 'input.json').read_text())
        reply = json.loads((folder / 'codec/call.json').read_text())['response']
        expanded = json.loads((folder / 'codec/expanded.json').read_text())
        wiki = json.loads((root / 'evidence/wiki-current.json').read_text())
        wiki['pages'] = [wiki['pages'][0]]
        wiki['references'] = []  # This fixture retains only one page, not its old cross-page links.
        wiki['pages'][0]['content'] = fixture['old_page']
        updated = copy.deepcopy(wiki); updated['pages'][0]['content'] = fixture['new_page']
        def model(open_reply):
            def answer(request):
                if request['operation'] == 'open': return open_reply
                if request['operation'] == 'phrase': return {'b': []}
                if request['operation'] == 'proposal': return {'p': [], 'existing': []}
                self.fail('Unexpected extension review')
            return answer
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); store = GitStore(base / 'store')
            profile = {'model': 'retained-fixture'}
            first = asyncio.run(ingest(store, base / 'first', [], recorded_at='2026-11-11T12:00:00Z',
                resume_wiki=wiki, model=model(fixture['previous_raw']), model_profile=profile))
            selected = first['candidate']['commit']
            second = asyncio.run(ingest(store, base / 'second', [], recorded_at='2026-11-11T12:01:00Z',
                previous_commit=selected, resume_wiki=updated, model=model(reply), model_profile=profile))
            saved = store.read(second['candidate']['commit'])
            trace = saved['extraction']['model_calls'][0]
            self.assertEqual(trace['mode'], 'statement_refs')
            self.assertEqual(json.loads(trace['native_response']), expanded)
            context = json.loads(trace['request']['messages'][1]['content'].split('\n\nCURRENT DOCUMENT:\n')[0])
            self.assertEqual(context['Previous statements (context only)']['s'], fixture['previous_raw']['s'])
            self.assertIn('-| Technician | Noor.[^2] |', context['Git diff (old to current, context only)'])
            self.assertEqual(saved['manifest']['status'], 'candidate')
            self.assertEqual(saved['manifest']['proposal_base_content'], selected)
            invalid = {**expanded, 's': [{'ref': 999999}]}
            with self.assertRaisesRegex(RuntimeError, 'invalid whole-statement reference'):
                asyncio.run(ingest(store, base / 'failed', [], recorded_at='2026-11-11T12:02:00Z',
                    previous_commit=selected, resume_wiki=updated, model=model(invalid), model_profile=profile))
            self.assertFalse((base / 'failed/candidate.json').exists())
            self.assertEqual(json.loads((base / 'failed/execution.json').read_text())['status'], 'failed')
            calls = json.loads((base / 'failed/model-calls.json').read_text())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['response'], invalid)

    def test_selected_git_snapshot_reuses_extraction_and_retains_call_log(self):
        import inspect
        from knowledge_atlas.core.ingest import ingest
        self.assertIn('model_profile',inspect.signature(ingest).parameters)
        root=Path(__file__).resolve().parent/'fixtures'
        wiki=json.loads((root/'evidence/wiki-current.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);store=GitStore(base/'store')
            def model(request):return {'e':[],'s':[],'n':[],'t':[]}
            first=asyncio.run(ingest(store,base/'first',[],recorded_at='2026-11-11T12:00:00Z',resume_wiki=wiki,model=model,model_profile={'model':'fixture'},extract_workers=2))
            log=json.loads((base/'first/model-calls.json').read_text())
            self.assertEqual(len(log),len(wiki['pages']))
            self.assertTrue(all('response' in c and 'elapsed_seconds' in c for c in log))
            second=asyncio.run(ingest(store,base/'second',[],recorded_at='2026-11-11T12:01:00Z',resume_wiki=wiki,
                previous_commit=first['candidate']['commit'],model=lambda r:self.fail('Reuse reached model'),model_profile={'model':'fixture'},extract_workers=2))
            saved=store.read(second['candidate']['commit'])
            self.assertEqual(saved['extraction']['cache']['reused_pages'],len(wiki['pages']))
            self.assertEqual(saved['manifest']['status'],'candidate')

    def test_parallel_transport_failure_retains_calls_without_candidate(self):
        import inspect,threading
        from knowledge_atlas.core.ingest import ingest
        self.assertIn('extract_workers',inspect.signature(ingest).parameters)
        root=Path(__file__).resolve().parent/'fixtures'
        wiki=json.loads((root/'evidence/wiki-current.json').read_text());wiki['pages']=wiki['pages'][:2]
        barrier=threading.Barrier(2);lock=threading.Lock();started=[]
        def model(request):
            with lock:
                index=len(started);started.append(request)
            barrier.wait(timeout=3)
            if index==0:raise RuntimeError('one page failed')
            return {'e':[],'s':[],'n':[],'t':[]}
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);store=GitStore(base/'store')
            with self.assertRaisesRegex(RuntimeError,'one page failed'):
                asyncio.run(ingest(store,base/'run',[],recorded_at='2026-11-11T12:00:00Z',resume_wiki=wiki,model=model,extract_workers=2))
            calls=json.loads((base/'run/model-calls.json').read_text())
            self.assertEqual(len(calls),2)
            self.assertEqual(sum('error' in c for c in calls),1)
            self.assertEqual(sum('response' in c for c in calls),1)
            self.assertFalse((base/'run/candidate.json').exists())
            self.assertFalse(store.root.exists())

@requires_native_utopia
class ExactRelationIngestTests(unittest.TestCase):
    def test_relation_reuse_comes_from_selected_git_and_uses_current_bundle_digest(self):
        import copy
        from knowledge_atlas.core.ingest import ingest
        from knowledge_atlas.core.extract_stage import extract_wiki
        from knowledge_atlas.core.graph_stage import records_for
        from knowledge_atlas.core.relations import digest
        root = Path(__file__).resolve().parent/'fixtures'
        wiki = json.loads((root/'evidence/wiki-current.json').read_text())
        page = wiki['pages'][0]
        quote = 'Claim is supported by Report if dry.'
        page['content'] += '\n\n' + quote
        wire = [quote, 'Claim', 'is supported by', 'Report', None, {'condition': 'dry'}, None, None]
        raw = extract_wiki({page['path'] + page['filename']: page['content']},
                           lambda _: {'e': [], 'n': [], 't': [], 's': [wire, wire]})
        self.assertEqual(len(raw['statements']), 2)
        profile = {'model': 'fixture', 'effort': 'high'}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); store = GitStore(base/'store')
            first = asyncio.run(ingest(store, base/'first', [], recorded_at='2026-11-11T12:00:00Z',
                resume_wiki=wiki, resume_extraction=raw, relation_model_profile=profile,
                model=lambda r: {'b': [[0, 'supports', 'reverse'], [1, 'supports', 'reverse']]}))
            selected = first['candidate']['commit']
            store.snapshot(wiki, {'entities': [], 'statements': []}, {}, recorded_at='2026-11-11T12:01:00Z')
            current = copy.deepcopy(raw)
            current['model_calls'][0]['elapsed_seconds'] += 1
            second = asyncio.run(ingest(store, base/'second', [], recorded_at='2026-11-11T12:02:00Z',
                previous_commit=selected, resume_wiki=wiki, resume_extraction=current,
                relation_model_profile=profile, model=lambda r: self.fail('Exact selected input called model')))
            saved = store.read(second['candidate']['commit'])
            self.assertEqual(saved['relations']['extraction_digest'], digest(current))
            self.assertEqual(saved['relations']['relations'][0]['subject'], 'Report')
            self.assertTrue(saved['relations']['model_calls'][0]['reused'])
            self.assertEqual(json.loads((base/'second/model-calls.json').read_text()), [])
            old_records = records_for(store.read(selected), selected)
            new_records = records_for(saved, second['candidate']['commit'])
            old_projected = {r['@id']: r for r in old_records if r['kind'] in ('statement', 'relation')}
            new_projected = {r['@id']: r for r in new_records if r['kind'] in ('statement', 'relation')}
            self.assertEqual(old_projected.keys(), new_projected.keys())
            self.assertEqual(sum(r['kind'] == 'statement' for r in new_projected.values()), 1)
            self.assertEqual({json.loads(r['payload'])['index'] for r in new_projected.values()
                              if r['kind'] == 'relation'}, {0, 1})


_DAMAGE = [
    pytest.param('source_id', None, id='missing-source-id'),
    pytest.param('recorded_at', None, id='missing-source-time'),
    pytest.param('passage_version', None, id='missing-passage-version'),
    pytest.param('passage_version', 2, id='unknown-passage-version'),
    pytest.param('passage_version', True, id='boolean-passage-version'),
]


def current_candidate(store):
    fixture = Path(__file__).parent/'fixtures/evidence'
    wiki = json.loads((fixture/'wiki-current.json').read_text())
    extraction = json.loads((fixture/'extraction-current.json').read_text())
    return store.snapshot(wiki, extraction, {}, recorded_at='2026-11-13T00:30:00Z')['commit']


def damaged_candidate(store, candidate, field, value, *, kind='knowledge'):
    from knowledge_atlas.core.git_store import git
    from knowledge_atlas.core.publication import commit_files
    from knowledge_atlas.core.relations import digest

    data = store.read(candidate)
    manifest = data['manifest']
    pin = manifest['modules'][kind]
    if kind == 'sources':
        source = store.module_path(store.root, pin)
        provenance = json.loads(git(source, 'show', pin['commit'] + ':provenance.json'))
        del provenance[0][field]
        files = {'provenance.json': provenance}
    elif field == 'passage_version':
        extraction = data['extraction']
        if value is None:
            del extraction[field]
        else:
            extraction[field] = value
        data['relations']['extraction_digest'] = digest(extraction)
        files = {'extraction.json': extraction, 'relations.json': data['relations']}
    else:
        del data['wiki']['sources'][0][field]
        files = {'wiki.json': data['wiki']}
    leaf = store.module_path(store.root, pin)
    old = pin['commit']
    pin['commit'] = commit_files(leaf, old, files, [old], 'Malformed retained fixture')
    git(leaf, 'update-ref', 'refs/heads/malformed', pin['commit'])
    bad = commit_files(store.root, candidate, {'manifest.json': manifest, 'relations.json': data['relations']},
                       [candidate], 'Pin malformed retained fixture', gitlinks={pin['mount']: pin['commit']})
    git(store.root, 'update-ref', 'refs/heads/malformed', bad)
    return bad, data, leaf


@pytest.mark.parametrize(('field', 'value'), _DAMAGE)
@pytest.mark.parametrize('entry_point', ['read', 'update', 'resume', 'projection'])
def test_retained_boundary_rejects_unsupported_material(tmp_path, field, value, entry_point):
    from knowledge_atlas.core.git_store import git
    from knowledge_atlas.core.graph_stage import records_for
    from knowledge_atlas.core.ingest import ingest

    store = GitStore(tmp_path/'atlas')
    good = current_candidate(store)
    bad, data, leaf = damaged_candidate(store, good, field, value)
    original = git(store.root, 'show-ref'), git(leaf, 'show-ref')
    with pytest.raises(ValueError, match=field):
        if entry_point == 'read':
            store.read(bad)
        elif entry_point in ('update', 'resume'):
            asyncio.run(ingest(
                store, tmp_path/'attempt', [], recorded_at='2026-11-13T02:00:00Z',
                previous_commit=bad if entry_point == 'update' else good,
                resume_wiki=data['wiki'], resume_extraction=data['extraction'],
                model=lambda _: pytest.fail('malformed retained input reached model'),
            ))
        else:
            records_for(data, bad)
    assert (git(store.root, 'show-ref'), git(leaf, 'show-ref')) == original
    assert not (tmp_path/'attempt/candidate.json').exists()
    assert store.read(good)['extraction']['passage_version'] == 1


@pytest.mark.parametrize(('field', 'value'), _DAMAGE)
def test_staged_restore_rejects_unsupported_retained_history(portable, tmp_path, field, value):
    check_damaged_restore(portable, tmp_path, 'knowledge', field, value)


def test_staged_restore_rejects_damaged_sources_leaf_only(portable, tmp_path):
    check_damaged_restore(portable, tmp_path, 'sources', 'source_id', None)


def check_damaged_restore(portable, tmp_path, kind, field, value):
    import zipfile

    from knowledge_atlas.core.archive import export_atlas, restore_atlas
    from knowledge_atlas.core.git_store import git, package_identity

    store, tokenizer, formatter, _, _, _ = portable
    candidate = current_candidate(store)
    good = tmp_path/'current.zip'
    manifest = export_atlas(store, good, tokenizer=tokenizer, formatter=formatter)
    bad, _, leaf = damaged_candidate(store, candidate, field, value, kind=kind)
    with pytest.raises(ValueError, match=field):
        store.read(bad)
    replacements = {}
    for repo in [store.root, leaf]:
        identity = package_identity(repo, 'HEAD')
        member = next(package['bundle'] for package in manifest['packages'] if package['package_id'] == identity)
        bundle = tmp_path/member
        git(repo, 'bundle', 'create', str(bundle), '--all')
        replacements[member] = bundle.read_bytes()
        manifest['sha256'][member] = hashlib.sha256(replacements[member]).hexdigest()
    damaged = tmp_path/'damaged.zip'
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(damaged, 'w') as archive:
        for name in original.namelist():
            payload = json.dumps(manifest) if name == 'archive.json' else replacements.get(name) or original.read(name)
            archive.writestr(name, payload)
    original_bytes = damaged.read_bytes()
    destination = tmp_path/'restored'
    with pytest.raises(ValueError, match=field):
        restore_atlas(damaged, destination)
    assert not destination.exists()
    assert damaged.read_bytes() == original_bytes
    assert git(store.root, 'rev-parse', 'refs/heads/malformed') == bad



@pytest.mark.parametrize(('name', 'field'), [
    ('wiki-initial-final.json', 'source_id'),
    ('extractor-output.json', 'passage_version'),
])
def test_historical_evidence_is_rejected_before_snapshot_write(tmp_path, name, field):
    fixture = Path(__file__).parent/'fixtures/evidence'
    path = fixture/name
    original = path.read_bytes()
    wiki = json.loads((fixture/'wiki-current.json').read_text())
    extraction = json.loads((fixture/'extraction-current.json').read_text())
    if field == 'source_id':
        wiki = json.loads(original)
    else:
        extraction = json.loads(original)
    store = GitStore(tmp_path/'atlas')
    with pytest.raises(ValueError, match=field):
        store.snapshot(wiki, extraction, {}, recorded_at='2026-11-13T00:30:00Z')
    assert not store.root.exists()
    assert path.read_bytes() == original
