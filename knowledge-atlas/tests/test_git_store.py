import copy
import json
from pathlib import Path
import tempfile
import unittest
from knowledge_atlas.core.git_store import GitStore, git

ROOT=Path(__file__).resolve().parent / 'fixtures'
class GitStoreTests(unittest.TestCase):
    def test_snapshot_pins_real_submodules_and_keeps_raw_wiki_and_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GitStore(Path(tmp)/'store')
            wiki=json.loads((ROOT/'evidence/wiki-initial-final.json').read_text())
            extraction=json.loads((ROOT/'evidence/extractor-output.json').read_text())
            result=store.snapshot(wiki,extraction,{'model_response':'retained response'},recorded_at='2026-11-09T21:00:00Z')
            self.assertIsNotNone(result)
            self.assertEqual(result['status'],'candidate')
            self.assertIsNone(result['accepted_at'])
            tree=git(store.root,'ls-tree','-r',result['commit'])
            self.assertIn('160000 commit',tree)
            saved=store.read(result['commit'])
            self.assertEqual(saved['wiki'],wiki)
            self.assertEqual(saved['extraction'],extraction)
            self.assertEqual(saved['evidence'],{'model_response':'retained response'})
            self.assertEqual(saved['sources']['review.txt'],(ROOT/'fixtures/printlab/review.txt').read_bytes())
            for key in ('sources','knowledge'):
                self.assertEqual(len(saved['manifest']['modules'][key]['commit']),40)

    def test_later_snapshot_does_not_change_old_time_or_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GitStore(Path(tmp)/'store')
            wiki=json.loads((ROOT/'evidence/wiki-initial-final.json').read_text())
            first=store.snapshot(wiki,{'statements':[]},{},recorded_at='2026-11-09T21:00:00Z')
            self.assertIsNotNone(first)
            newer=copy.deepcopy(wiki);newer['recorded_at']='2026-11-10T21:00:00Z'
            second=store.snapshot(newer,{'statements':[]},{},recorded_at='2026-11-10T21:00:00Z')
            self.assertNotEqual(first['commit'],second['commit'])
            self.assertEqual(store.read(first['commit'])['manifest']['recorded_at'],'2026-11-09T21:00:00Z')
            self.assertEqual(store.read(first['commit'])['wiki'],wiki)
            with self.assertRaises(ValueError):store.read('HEAD')
