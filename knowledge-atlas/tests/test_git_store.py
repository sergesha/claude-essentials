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


def test_unmarked_empty_git_project_is_rejected_untouched(tmp_path):
    import pytest
    import uuid
    from knowledge_atlas.project_registry import repository_identity
    from knowledge_atlas.contracts import AtlasError
    from knowledge_atlas.core.git_store import _commit
    store = GitStore(tmp_path/'existing')
    store.root.mkdir(parents=True)
    git(store.root, 'init', '-qb', 'data')
    owner = str(uuid.uuid4())
    (store.root/'package.json').write_text(json.dumps({'package_id': owner}))
    (store.root/'manifest.json').write_text(json.dumps({'package_id': owner, 'modules': {},
        'status': 'candidate', 'proposal_base_publication': None, 'accepted_at': None,
        'recorded_at':'2026-11-13T00:00:00Z'}))
    commit = _commit(store.root, 'Unmarked empty project')
    with pytest.raises(AtlasError, match='invalid_atlas'):
        repository_identity(store.base)
    with pytest.raises(ValueError):
        store.read(commit)
    assert git(store.root, 'rev-parse', 'HEAD') == commit
    assert git(store.root, 'status', '--porcelain') == ''


def test_recompilation_validates_both_owned_repositories_before_writes(tmp_path):
    import pytest
    from knowledge_atlas.core.git_store import _commit
    from test_packages import snapshot
    store = GitStore(tmp_path/'project')
    original = store.read(snapshot(store))
    knowledge = store.base/'modules/knowledge'
    marker = json.loads((knowledge/'package.json').read_text())
    del marker['atlas_format']
    (knowledge/'package.json').write_text(json.dumps(marker))
    _commit(knowledge, 'Unmarked knowledge repository')
    source = store.base/'modules/sources'
    before = [git(path, 'show-ref') for path in (store.root, source, knowledge)]
    wiki = original['wiki']
    wiki['sources'][0]['recorded_at'] = '2026-11-14T00:00:00Z'
    with pytest.raises(ValueError):
        store.snapshot(wiki, original['extraction'], {}, recorded_at='2026-11-14T00:00:00Z',
                       relation_previous=original['relations'])
    assert before == [git(path, 'show-ref') for path in (store.root, source, knowledge)]
