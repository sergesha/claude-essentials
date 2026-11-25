import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest
from acceptance_fixture import review_acceptance
from knowledge_atlas.core.git_store import GitStore, git
from knowledge_atlas.core.knowledge import propose_records
from knowledge_atlas.core.publication import Publications, commit_files
from knowledge_atlas.core.reader import AtlasReader
from knowledge_atlas.core.graph_stage import records_for

ROOT = Path(__file__).resolve().parent / 'fixtures'


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GitStore(Path(self.tmp.name) / 'store')
        self.wiki = json.loads((ROOT / 'evidence/wiki-current.json').read_text())
        self.extract = json.loads((ROOT / 'evidence/extraction-current.json').read_text())
        self.candidate = self.store.snapshot(self.wiki, self.extract, {}, recorded_at='2026-11-09T21:00:00Z')['commit']
        self.pub = Publications(self.store)

    def next_candidate(self, publication):
        return propose_records(self.store, self.pub.read(publication)['content_commit'], [],
            actor='Fixture', method='Fixture', reason='Reviewed revision',
            recorded_at='2026-11-10T00:00:00Z', base_publication=publication)

    def review(self, base=None, actor='Synthetic reviewer A', *, candidate=None, when='2026-11-10T00:00:00Z'):
        return review_acceptance(self.pub, candidate or self.candidate, {'decision': 'accept', 'actor': actor,
            'kind': 'synthetic', 'reason': 'Lifecycle exercise only, not human practical acceptance',
            'accepted_at': when, 'base_publication': base},
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
        accepted = review_acceptance(self.pub, changed, fresh, profile)
        self.assertEqual(self.store.read(accepted)['manifest']['acceptance']['candidate_commit'], changed)

    def test_failed_projection_preserves_ready_and_publication_has_real_parent_edges(self):
        first_content = self.review()
        first = self.pub.publish(first_content, self.projection)
        self.assertEqual(self.pub.ready(), first)
        second_content = self.review(first, candidate=self.next_candidate(first))
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
            candidate = self.next_candidate(self.pub.ready()) if self.pub.ready() else self.candidate
            content = review_acceptance(self.pub, candidate, {'decision': 'accept', 'actor': 'Synthetic time reviewer',
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


    def test_first_native_candidate_cannot_replace_later_ready(self):
        import asyncio
        from knowledge_atlas.core.ingest import ingest
        initial = asyncio.run(ingest(self.store, Path(self.tmp.name) / 'native', [],
            recorded_at='2026-11-09T21:00:00Z', resume_wiki=self.wiki,
            resume_extraction={'entities': [], 'statements': []}, model=self.fail))['candidate']['commit']
        first = self.pub.publish(self.review(candidate=initial), self.projection)
        second = self.pub.publish(self.review(first, candidate=self.next_candidate(first)), self.projection)
        with self.assertRaisesRegex(ValueError, 'stale proposal'):
            self.review(second, candidate=initial)
        self.assertEqual(self.pub.ready(), second)
        self.assertIsNone(self.store.read(initial)['manifest']['proposal_base_publication'])

    def test_legacy_unbound_candidate_requires_a_new_proposal(self):
        manifest = self.store.read(self.candidate)['manifest']
        manifest.pop('proposal_base_publication', None)
        legacy = commit_files(self.store.root, self.candidate, {'manifest.json': manifest},
                              [self.candidate], 'Fixture: legacy candidate')
        with self.assertRaisesRegex(ValueError, 'stale proposal'):
            self.review(candidate=legacy)
        self.assertIsNone(self.pub.ready())
        first = self.pub.publish(self.review(), self.projection)
        with self.assertRaisesRegex(ValueError, 'stale proposal'):
            self.review(first, candidate=legacy)
        self.assertEqual(self.pub.ready(), first)

    def test_channel_is_preserved_and_pre_channel_publications_are_rejected(self):
        profile = json.loads((ROOT / 'embedding-profile.json').read_text())
        decision = {'decision':'accept', 'actor':'Fixture', 'kind':'synthetic', 'reason':'Channel test',
                    'attribution_channel':'mcp', 'base_publication':None, 'accepted_at':'2026-11-10T00:00:00Z'}
        content = review_acceptance(self.pub, self.candidate, decision, profile)
        current = self.pub.publish(content, self.projection)
        receipt = self.pub.read(current)
        self.assertEqual(receipt['acceptance_channel'], 'mcp')
        conflicting = commit_files(self.store.root, current, {'publication.json': {**receipt, 'acceptance_channel': 'viewer'}},
                                   [content], 'Fixture: inconsistent attribution')
        with self.assertRaisesRegex(ValueError, 'metadata disagrees'):
            self.pub.read(conflicting)
        del receipt['acceptance_channel']
        legacy = commit_files(self.store.root, current, {'publication.json':receipt}, [content], 'Legacy receipt')
        with self.assertRaisesRegex(ValueError, 'channel|metadata'):
            self.pub.read(legacy)

    def test_acceptance_order_compares_instants_and_rechecks_pending_content(self):
        first = self.pub.publish(self.review(), self.projection)
        candidate = self.next_candidate(first)
        before = '2026-11-10T00:59:59+01:00'
        with self.assertRaisesRegex(ValueError, 'accepted_at'):
            self.review(first, candidate=candidate, when=before)
        equal = self.review(first, candidate=candidate, when='2026-11-10T01:00:00+01:00')
        with patch.object(Publications, '_check_acceptance_time'):
            legacy = self.review(first, candidate=candidate, when=before)
        with self.assertRaisesRegex(ValueError, 'accepted_at'):
            self.pub.publish(legacy, lambda _: self.fail('Must reject before building'))
        self.assertEqual(self.pub.ready(), first)
        receipt = lambda c: {**self.projection(c), 'database': 'admin/fixture'}
        second = self.pub.publish(equal, receipt)
        reader = lambda **kw: AtlasReader(self.store, 'http://unused.invalid', set, **kw)
        self.assertEqual(reader(as_known_at='2026-11-10T00:00:00Z').publication, second)
        # The pre-monotonicity format can contain genuine, out-of-order reviews.
        with patch.object(Publications, '_check_acceptance_time'):
            content = self.review(second, candidate=self.next_candidate(second), when=before)
            broken = self.pub.publish(content, receipt)
        for selector in ({'as_known_at': before}, {'publication': broken}, {}):
            with self.assertRaisesRegex(ValueError, 'precedes'):
                reader(**selector)
        self.assertEqual(self.pub.ready(), broken)

    def test_future_acceptance_rejected_before_review_and_projection(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, 'future'):
            self.review(when=future)
        self.assertFalse(git(self.store.root, 'for-each-ref', 'refs/poc9/reviews/'))
        # Old software could have saved this accepted content before upgrading.
        with patch.object(Publications, '_check_acceptance_time'):
            content = self.review(when=future)
        with self.assertRaisesRegex(ValueError, 'future'):
            self.pub.publish(content, lambda _: self.fail('unexpected projection'))
        self.assertIsNone(self.pub.ready())

    def test_acceptance_clock_allows_exactly_five_minutes_of_skew(self):
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        boundary = now + timedelta(minutes=5)
        with patch('knowledge_atlas.core.publication.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            Publications._check_acceptance_time({'accepted_at': boundary.isoformat()}, None)
            with self.assertRaisesRegex(ValueError, 'future'):
                Publications._check_acceptance_time({'accepted_at': (boundary + timedelta(microseconds=1)).isoformat()}, None)

    def test_retained_future_acceptance_fails_without_rewriting_history(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        project = lambda c: {**self.projection(c), 'database': 'admin/fixture'}
        with patch.object(Publications, '_check_acceptance_time'):
            old = self.pub.publish(self.review(when=future), project)
        saved = git(self.store.root, 'show', old + ':publication.json')
        published = datetime.fromisoformat(json.loads(saved)['published_at'])
        with self.assertRaisesRegex(ValueError, 'future'):
            AtlasReader(self.store, 'http://unused.invalid', set, as_known_at=published.isoformat())
        self.assertEqual(git(self.store.root, 'show', old + ':publication.json'), saved)

    def test_future_wrapper_clock_cannot_validate_an_impossible_acceptance(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with patch.object(Publications, '_check_acceptance_time'):
            content = self.review(when=future)
            wrapper = self.pub.publish(content, self.projection)
        receipt = json.loads(git(self.store.root, 'show', wrapper + ':publication.json'))
        receipt['published_at'] = future
        forged = commit_files(self.store.root, content, {'publication.json': receipt}, [content], 'Future clocks')
        with self.assertRaisesRegex(ValueError, 'future'):
            self.pub.read(forged)

    def test_publication_read_requires_accepted_content(self):
        content = self.review()
        good = self.pub.publish(content, self.projection)
        receipt = {**self.pub.read(good), 'content_commit': self.candidate}
        inconsistent = commit_files(self.store.root, self.candidate, {'publication.json': receipt},
                                    [self.candidate], 'Fixture: inconsistent retained snapshot')
        with self.assertRaisesRegex(ValueError, 'accepted content'):
            self.pub.read(inconsistent)
        with patch.object(Publications, 'ready', return_value=inconsistent):
            with self.assertRaisesRegex(ValueError, 'accepted content'):
                AtlasReader(self.store, 'http://unused.invalid', set)

    def test_publication_requires_direct_parent_named_in_the_review(self):
        content = self.review()
        # A metadata-only commit inherits the review but has a different parent.
        copy = commit_files(self.store.root, content, {}, [content], 'Fixture: copied review')
        with self.assertRaisesRegex(ValueError, 'reviewed candidate'):
            self.pub.publish(copy, lambda _: self.fail('unexpected projection'))
        self.assertIsNone(self.pub.ready())

    def test_completed_decision_wrapper_preserves_candidate_tree_and_effect_parents(self):
        from knowledge_atlas.core.decision_receipts import decision_inventory
        content = self.review()
        publication = self.pub.publish(content, self.projection)
        entry, = decision_inventory(self.store)
        parents = git(self.store.root, 'show', '-s', '--format=%P', entry['commit']).split()
        for damage in ('tree', 'parents'):
            with self.subTest(damage=damage):
                forged = commit_files(self.store.root, entry['commit'], {},
                                      parents if damage == 'tree' else [entry['commit']],
                                      'Malformed decision wrapper',
                                      remove_paths=['manifest.json'] if damage == 'tree' else [])
                git(self.store.root, 'update-ref', entry['ref'], forged)
                with self.assertRaisesRegex(ValueError, 'decision'):
                    self.pub.read(publication)
                git(self.store.root, 'update-ref', entry['ref'], entry['commit'])
