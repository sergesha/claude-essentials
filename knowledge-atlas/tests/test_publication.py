import json
from pathlib import Path
import tempfile
import unittest
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.reader import AtlasReader
from knowledge_atlas.core.graph_stage import records_for

ROOT = Path(__file__).resolve().parent / 'fixtures'


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GitStore(Path(self.tmp.name) / 'store')
        self.wiki = json.loads((ROOT / 'evidence/wiki-initial-final.json').read_text())
        self.extract = json.loads((ROOT / 'evidence/extractor-output.json').read_text())
        self.candidate = self.store.snapshot(self.wiki, self.extract, {}, recorded_at='2026-11-09T21:00:00Z')['commit']
        self.pub = Publications(self.store)

    def review(self, base=None, actor='Synthetic reviewer A'):
        return self.pub.review(self.candidate, {'decision': 'accept', 'actor': actor,
            'kind': 'synthetic', 'reason': 'Lifecycle exercise only, not human practical acceptance',
            'accepted_at': '2026-11-10T00:00:00Z', 'base_publication': base},
            json.loads((ROOT / 'embedding-profile.json').read_text()))

    @staticmethod
    def projection(content):
        # External builder substitute: this test proves Git publication, not native indexing.
        return {'content_commit': content, 'graph_commit': 'synthetic-graph',
                'search_checkpoint': 'synthetic-graph', 'ready': True, 'test_only': True,
                'profile_id': '58f7a75c062da9eaefa08ae62c0af6424a17cad11adbe7230fff652f53a9ceeb'}

    def test_review_is_git_history_and_does_not_publish(self):
        content = self.review()
        saved = self.store.read(content)
        self.assertEqual(saved['manifest']['status'], 'accepted')
        self.assertEqual(saved['manifest']['accepted_at'], '2026-11-10T00:00:00Z')
        decision = json.loads(git(self.store.root, 'show', content + ':review.json'))
        self.assertEqual(decision['candidate_commit'], self.candidate)
        self.assertEqual(decision['kind'], 'synthetic')
        self.assertIsNone(self.pub.ready())
        self.assertEqual(self.store.read(self.candidate)['manifest']['status'], 'candidate')

    def test_recorded_review_cannot_accept_different_candidate_content(self):
        content = self.review()
        decision = json.loads(git(self.store.root, 'show', content + ':review.json'))
        self.wiki['pages'][0]['content'] += '\nA changed proposal requires its own review.\n'
        changed = self.store.snapshot(self.wiki, self.extract, {}, recorded_at='2026-11-10T01:00:00Z')['commit']
        before = git(self.store.root, 'for-each-ref', '--format=%(objectname)', 'refs/poc9/reviews/')
        profile = json.loads((ROOT / 'embedding-profile.json').read_text())
        with self.assertRaisesRegex(ValueError, 'new content requires review'):
            self.pub.review(changed, decision, profile)
        self.assertEqual(git(self.store.root, 'for-each-ref', '--format=%(objectname)', 'refs/poc9/reviews/'), before)
        self.assertIsNone(self.pub.ready())
        self.assertEqual(self.store.read(changed)['manifest']['status'], 'candidate')
        # An explicitly new decision for the changed candidate remains possible.
        fresh = {**decision, 'candidate_commit': changed, 'reason': 'New review of changed proposal'}
        accepted = self.pub.review(changed, fresh, profile)
        self.assertEqual(self.store.read(accepted)['manifest']['acceptance']['candidate_commit'], changed)

    def test_failed_projection_preserves_ready_and_publication_has_real_parent_edges(self):
        first_content = self.review()
        first = self.pub.publish(first_content, self.projection)
        self.assertEqual(self.pub.ready(), first)
        second_content = self.review(first)
        def interrupted(_):
            raise RuntimeError('projection interrupted')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.pub.publish(second_content, interrupted)
        self.assertEqual(self.pub.ready(), first)
        second = self.pub.publish(second_content, self.projection)
        parents = git(self.store.root, 'show', '-s', '--format=%P', second).split()
        self.assertEqual(parents, [first, second_content])
        self.assertEqual(json.loads(git(self.store.root, 'show', first + ':publication.json'))['content_commit'], first_content)

    def test_competing_review_cannot_overwrite_winner_and_retry_reconciles(self):
        a = self.review(actor='Synthetic A')
        b = self.review(actor='Synthetic B')
        winner = []
        def racing_builder(content):
            winner.append(self.pub.publish(b, self.projection))
            return self.projection(content)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.pub.publish(a, racing_builder)
        first = winner[0]
        self.assertEqual(self.pub.ready(), first)
        self.assertEqual(self.pub.publish(b, self.projection), first)

    def test_candidate_or_incomplete_projection_cannot_be_ready(self):
        with self.assertRaises(ValueError):
            self.pub.publish(self.candidate, self.projection)
        content = self.review()
        with self.assertRaises(ValueError):
            self.pub.publish(content, lambda c: {**self.projection(c), 'ready': False})
        self.assertIsNone(self.pub.ready())

    def test_different_projection_profile_cannot_be_relabelled_and_published(self):
        content = self.review()
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.pub.publish(content, lambda c: {**self.projection(c), 'profile_id': 'different-profile'})
        self.assertIsNone(self.pub.ready())

    def test_reader_selects_retained_acceptance_time_without_using_commit_clock(self):
        profile = json.loads((ROOT / 'embedding-profile.json').read_text())
        publications = []
        for when in ('2026-03-10T10:00:00Z', '2026-04-20T10:00:00Z'):
            content = self.pub.review(self.candidate, {'decision': 'accept', 'actor': 'Synthetic time reviewer',
                'kind': 'synthetic', 'reason': 'Imported fixture axis, not server clock',
                'accepted_at': when, 'base_publication': self.pub.ready()}, profile)
            publications.append(self.pub.publish(content, lambda c: {**self.projection(c), 'database': 'admin/test-only'}))
        allowed = {r['@id'] for r in records_for(self.store.read(content), content)}
        def read(when):
            try:
                return AtlasReader(self.store, 'http://unused.invalid', lambda: allowed, as_known_at=when)
            except TypeError as error:
                self.fail('S08 acceptance-time read is unavailable: ' + str(error))
        old = read('2026-03-15T00:00:00Z')
        self.assertEqual(old.publication, publications[0])
        self.assertEqual(read('2026-04-25T00:00:00Z').publication, publications[1])
        self.assertEqual(read('2026-04-20T12:00:00+02:00').publication, publications[1])
        with self.assertRaisesRegex(ValueError, 'no published slice'):
            read('2026-03-01T00:00:00Z')
        source = next(rid for rid in allowed if rid.startswith('Record/source-'))
        self.assertIsNotNone(old.retrieve_evidence(source))
        allowed.remove(source)
        self.assertIsNone(old.retrieve_evidence(source))
        self.assertEqual(self.pub.ready(), publications[1])
