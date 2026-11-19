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
pytestmark = pytest.mark.skipif(not os.environ.get('POC9_EXTRACT_BIN'), reason='explicit native Utopia binary required')

class EfficientIngestTests(unittest.TestCase):
    def test_selected_git_page_drives_codec_candidate_and_bad_reply_stops_snapshot(self):
        import copy
        from knowledge_atlas.core.ingest import ingest
        root = Path(__file__).resolve().parent/'fixtures'
        folder = root / 'evidence/native-components/row-reference-probe/run-statements-only'
        fixture = json.loads((folder / 'input.json').read_text())
        reply = json.loads((folder / 'codec/call.json').read_text())['response']
        expanded = json.loads((folder / 'codec/expanded.json').read_text())
        wiki = json.loads((root / 'evidence/wiki-initial-final.json').read_text())
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
        wiki=json.loads((root/'evidence/wiki-initial-final.json').read_text())
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
        wiki=json.loads((root/'evidence/wiki-initial-final.json').read_text());wiki['pages']=wiki['pages'][:2]
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

class ExactRelationIngestTests(unittest.TestCase):
    def test_relation_reuse_comes_from_selected_git_and_uses_current_bundle_digest(self):
        import copy
        from knowledge_atlas.core.ingest import ingest
        from knowledge_atlas.core.relations import digest
        root = Path(__file__).resolve().parent/'fixtures'
        wiki = json.loads((root/'evidence/wiki-initial-final.json').read_text())
        raw = {'entities': [], 'statements': [{'subject': 'Claim', 'phrase': 'is supported by',
            'object': 'Report', 'value': None, 'quote': 'Claim is supported by Report if dry.',
            'qualifiers': [['condition', 'dry']], 'when': None, 'ended': None}]}
        profile = {'model': 'fixture', 'effort': 'high'}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); store = GitStore(base/'store')
            first = asyncio.run(ingest(store, base/'first', [], recorded_at='2026-11-11T12:00:00Z',
                resume_wiki=wiki, resume_extraction=raw, relation_model_profile=profile,
                model=lambda r: {'b': [[0, 'supports', 'reverse']]}))
            selected = first['candidate']['commit']
            store.snapshot(wiki, {'entities': [], 'statements': []}, {}, recorded_at='2026-11-11T12:01:00Z')
            current = copy.deepcopy(raw); current['model_calls'] = [{'elapsed_seconds': 1}]
            second = asyncio.run(ingest(store, base/'second', [], recorded_at='2026-11-11T12:02:00Z',
                previous_commit=selected, resume_wiki=wiki, resume_extraction=current,
                relation_model_profile=profile, model=lambda r: self.fail('Exact selected input called model')))
            saved = store.read(second['candidate']['commit'])
            self.assertEqual(saved['relations']['extraction_digest'], digest(current))
            self.assertEqual(saved['relations']['relations'][0]['subject'], 'Report')
            self.assertTrue(saved['relations']['model_calls'][0]['reused'])
            self.assertEqual(json.loads((base/'second/model-calls.json').read_text()), [])
