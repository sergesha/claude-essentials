import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from acceptance_fixture import review_acceptance
from knowledge_atlas.core.git_store import GitStore
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.graph_stage import records_for, GraphStage, key
from knowledge_atlas.core.native_projection import NativeProjection
from knowledge_atlas.core.reader import AtlasReader

ROOT = Path(__file__).parent/'fixtures'


def test_revision_support_propagates_once_across_shared_grounds():
    from knowledge_atlas.core.knowledge import _unchanged_support

    class Group(list):
        visits = 0

        def __iter__(self):
            Group.visits += 1
            return super().__iter__()

    def active(*groups):
        return {'state': 'active', 'support_sets': [Group(group) for group in groups]}

    records = {'trial': active(), 'withdrawn': {'state': 'withdrawn'}, 'changed': active(),
               'loop': active(['grounded']), 'grounded': active(['loop'], ['trial', 'trial']),
               'closed': active(['closed']), 'missing': active(['absent']), 'empty': active([]),
               'requires_both': active(['trial', 'withdrawn']),
               'blocked': {**active(['trial']), 'depends_on': ['affected']}}
    supported = {'trial', 'loop', 'grounded'}
    previous = 'grounded'
    for level in range(12):
        left, right, both = (f'{level}-{side}' for side in ('left', 'right', 'both'))
        records[left] = active([previous])
        records[right] = active([previous])
        records[both] = active([left, right])
        supported.update([left, right, both])
        previous = both
    assert _unchanged_support(records, {'changed'}, {'affected'}) == supported
    assert Group.visits <= sum(len(record.get('support_sets', [])) for record in records.values())


def proposal_records(snapshot):
    source = snapshot['sources']['review.txt'].decode()
    quote = source.split('\n')[0]
    provenance = [{'record_id': 'Record/'+key('source', 'review.txt'),
                   'start': 0, 'end': len(quote), 'quote': quote}]
    conditions = {'feedstock': 'dried', 'enclosure': 'closed', 'ambient_c': {'min': 20, 'max': 24}}
    return [
        {'id': 'p14', 'kind': 'episode', 'text': 'P-14 favored Luma for warping under the stated conditions.',
         'episode_id': 'P-14', 'conditions': conditions, 'provenance': provenance},
        {'id': 'p22', 'kind': 'episode', 'text': 'Independent P-22 favored Luma for warping under the same conditions.',
         'episode_id': 'P-22', 'conditions': conditions, 'provenance': provenance},
        {'id': 'warping', 'kind': 'lesson', 'text': 'Prefer Luma for QL-7 warping only under the stated trial conditions; no strength guarantee.',
         'conditions': conditions, 'support_sets': [['p14'], ['p22']], 'provenance': provenance},
    ]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store = GitStore(Path(self.tmp.name)/'store')
        self.base = self.store.snapshot(json.loads((ROOT/'evidence/wiki-current.json').read_text()),
            json.loads((ROOT/'evidence/extraction-current.json').read_text()), {}, recorded_at='2026-11-10T00:00:00Z')['commit']
        self.profile = json.loads((ROOT/'embedding-profile.json').read_text())

    def propose(self, base, entries, publication=None):
        from knowledge_atlas.core.knowledge import propose_records
        return propose_records(self.store, base, entries, actor='Interactive synthetic fixture author',
            method='explicit source-grounded curation', reason='Exercise the approved application cycle',
            recorded_at='2026-11-10T00:01:00Z', base_publication=publication)

    def review(self, candidate, publication=None):
        return review_acceptance(Publications(self.store), candidate, {'decision': 'accept', 'kind': 'synthetic',
            'actor': 'Synthetic test reviewer', 'reason': 'Controlled integration fixture, not human acceptance',
            'accepted_at': '2026-11-10T00:02:00Z', 'base_publication': publication}, self.profile)

    def test_semantic_records_are_git_candidates_with_exact_provenance_and_native_edges(self):
        original = self.store.read(self.base)
        proposal = self.propose(self.base, proposal_records(original))
        self.assertIsInstance(proposal, str)
        snapshot = self.store.read(proposal)
        self.assertEqual(snapshot['sources'], original['sources'])
        self.assertEqual(snapshot['manifest']['modules'], original['manifest']['modules'])
        self.assertEqual(len(snapshot['knowledge_records']), 3)
        self.assertIsNone(Publications(self.store).ready())
        candidates = [r for r in records_for(snapshot, proposal) if r['kind'] == 'lesson']
        self.assertEqual(candidates[0]['status'], 'candidate')
        content = self.review(proposal)
        projected = records_for(self.store.read(content), content)
        lesson = next(r for r in projected if r['kind'] == 'lesson')
        episodes = [r for r in projected if r['kind'] == 'episode']
        self.assertEqual(lesson['status'], 'accepted')
        self.assertTrue({r['@id'] for r in episodes}.issubset(lesson['links']))
        self.assertEqual(json.loads(lesson['payload'])['provenance'][0]['source_commit'], self.base)
        self.assertEqual(json.loads(lesson['payload'])['author'], 'Interactive synthetic fixture author')
        self.assertNotIn('knowledge_records', original)

    def test_source_author_survives_curation_and_review(self):
        from knowledge_atlas.core.git_store import git
        entries = proposal_records(self.store.read(self.base))
        entries[0]['author'] = 'Ivo'
        entries[1]['author'] = None  # Explicitly unknown is not the curator.
        proposal = self.propose(self.base, entries)
        snapshot = self.store.read(proposal)
        authors = {r['id']: r['author'] for r in snapshot['knowledge_records']}
        self.assertEqual(authors['p14'], 'Ivo')
        self.assertIsNone(authors['p22'])
        self.assertEqual(authors['warping'], 'Interactive synthetic fixture author')
        self.assertEqual(json.loads(git(self.store.root, 'show', proposal+':proposal.json'))['actor'],
                         'Interactive synthetic fixture author')
        content = self.review(proposal)
        payloads = [json.loads(r['payload']) for r in records_for(self.store.read(content), content)
                    if r['@id'].startswith('Record/knowledge-')]
        self.assertEqual({r['local_id']: r['author'] for r in payloads}, authors)
        self.assertIsNone(Publications(self.store).ready())


    def test_reviewed_missing_premise_enables_unchanged_rule_only_after_acceptance(self):
        from knowledge_atlas.core.applicability import assess
        entries = proposal_records(self.store.read(self.base))
        entries[2]['support_sets'] = [['missing-trial']]
        entries.append(dict(entries[2], id='nested', support_sets=[['warping']]))
        first = self.review(self.propose(self.base, entries))

        def assessment(commit):
            records = [json.loads(r['payload']) for r in records_for(self.store.read(commit), commit)
                       if r['@id'].startswith('Record/knowledge-')]
            by_id = {r['id']: r for r in records}
            target = next(r['id'] for r in records if r['local_id'] == 'nested')
            return assess(by_id, target, {'feedstock': 'dried', 'enclosure': 'closed', 'ambient_c': 22})

        self.assertEqual(assessment(first)['status'], 'insufficient')
        addition = dict(entries[1], id='missing-trial')
        proposal = self.propose(first, [addition])
        self.assertEqual(assessment(proposal)['status'], 'needs_review')
        accepted = self.review(proposal)
        self.assertEqual(assessment(accepted)['status'], 'supported')
        self.assertEqual(assessment(first)['status'], 'insufficient')
        withdrawn = self.review(self.propose(accepted, [dict(addition, state='withdrawn', reason='Trial withdrawn')]))
        self.assertEqual(assessment(withdrawn)['status'], 'needs_review')

    def test_inexact_quote_is_rejected_before_candidate_creation(self):
        entries = proposal_records(self.store.read(self.base))
        entries[0]['provenance'][0]['quote'] = 'Unsupported words'
        with self.assertRaisesRegex(ValueError, 'quote'):
            self.propose(self.base, entries)

    def test_withdrawn_or_branch_preserves_nested_alternative_and_marks_wiki_impact(self):
        entries = proposal_records(self.store.read(self.base))
        entries += [dict(entries[2], id='nested', support_sets=[['warping']]),
                    dict(entries[2], id='summary', kind='wiki', support_sets=[], depends_on=['warping'])]
        first = self.review(self.propose(self.base, entries))
        change = dict(entries[0], state='withdrawn', reason='Trial evidence withdrawn')
        revised = self.review(self.propose(first, [change]))
        records = [json.loads(r['payload']) for r in records_for(self.store.read(revised), revised)
                   if r['@id'].startswith('Record/knowledge-')]
        by_local = {r['local_id']:r for r in records}
        self.assertEqual(by_local['warping']['status'], 'accepted')
        self.assertEqual(by_local['nested']['status'], 'accepted')
        self.assertEqual(by_local['summary']['status'], 'needs_review')
        self.assertIn('impact', by_local['warping'])
        from knowledge_atlas.core.applicability import assess
        result = assess({r['id']:r for r in records}, by_local['nested']['id'],
                        {'feedstock':'dried', 'enclosure':'closed', 'ambient_c':22})
        self.assertEqual(result['status'], 'supported')
        self.assertEqual(result['independent_episodes'], ['P-22'])


