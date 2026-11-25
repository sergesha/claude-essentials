import json
import hashlib
from pathlib import Path
import unittest
from test_retained_knowledge import KnowledgeTests, proposal_records
from knowledge_atlas.core.git_store import git
from knowledge_atlas.core.publication import Publications
from knowledge_atlas.core.graph_stage import records_for, key


class FeedbackTests(KnowledgeTests):
    # Reuse only setup/helpers; inherited behavior checks belong to their suite.
    test_semantic_records_are_git_candidates_with_exact_provenance_and_native_edges = None
    test_inexact_quote_is_rejected_before_candidate_creation = None
    test_withdrawn_or_branch_preserves_nested_alternative_and_marks_wiki_impact = None
    test_current_rights_follow_historical_citation_not_replaced_current_provenance = None
    test_published_answer_and_withdrawal_preserve_old_slice_and_current_access = None

    def test_outcome_is_an_attributed_candidate_and_does_not_rewrite_claim(self):
        from knowledge_atlas.core.feedback import record_case_outcome
        content = self.review(self.propose(self.base, proposal_records(self.store.read(self.base))))
        lesson = next(r for r in records_for(self.store.read(content), content) if r['kind']=='lesson')
        raw = '{"case_id":"P-40", "description":"New brackets warped; humidity was not measured.", "result":"failed", "context":{"feedstock":"dried","enclosure":"closed","ambient_c":22}, "observed_at":null}'
        # The supplied application origin is retained even before this isolated
        # Git-level fixture has a READY; the integration test exercises publication.
        candidate = record_case_outcome(self.store, content, lesson['@id'], raw,
            actor='Synthetic operator', recorded_at='2026-11-10T01:00:00Z', base_publication=None)
        self.assertIsInstance(candidate, str)
        snapshot = self.store.read(candidate)
        outcome = next(r for r in snapshot['knowledge_records'] if r['kind']=='outcome')
        self.assertEqual(outcome['raw_report'], raw)
        self.assertEqual(outcome['report']['result'], 'failed')
        self.assertIsNone(outcome['source_time'])
        self.assertEqual(outcome['provenance'][0]['role'], 'applied_claim')
        self.assertEqual(outcome['provenance'][0]['source_commit'], content)
        self.assertEqual(next(r for r in snapshot['knowledge_records'] if r['kind']=='lesson')['state'], 'active')
        self.assertEqual(snapshot['manifest']['status'], 'candidate')
        self.assertIsNone(Publications(self.store).ready())
        self.assertEqual(json.loads(git(self.store.root,'show',candidate+':proposal.json'))['revision_targets'], [lesson['@id']])

    def test_native_statement_outcome_preserves_applied_record_without_creating_lesson(self):
        from knowledge_atlas.core.feedback import record_case_outcome
        applied = next(r for r in records_for(self.store.read(self.base), self.base) if r['kind']=='statement')
        candidate = record_case_outcome(self.store, self.base, applied['@id'],
            '{"case_id":"native-1","description":"Reported local result; cause unknown","context":{}}',
            actor='Synthetic operator', recorded_at='2026-11-10T01:00:00Z')
        snapshot = self.store.read(candidate)
        self.assertEqual([r['kind'] for r in snapshot['knowledge_records']], ['outcome'])
        outcome = snapshot['knowledge_records'][0]
        self.assertEqual(outcome['applies_to'], applied['@id'])
        self.assertEqual(outcome['provenance'][0]['quote'], applied['text'])
        self.assertEqual(outcome['provenance'][0]['source_commit'], self.base)
        self.assertIsNone(Publications(self.store).ready())

    def test_reviewable_revision_keeps_feedback_and_changes_real_wiki_package(self):
        from knowledge_atlas.core.feedback import record_case_outcome, revise_compilation
        content = self.review(self.propose(self.base, proposal_records(self.store.read(self.base))))
        lesson = next(r for r in records_for(self.store.read(content), content) if r['kind']=='lesson')
        feedback = record_case_outcome(self.store, content, lesson['@id'],
            '{"case_id":"P-40","description":"Warping recurred","result":"failed","context":{}}',
            actor='Synthetic operator', recorded_at='2026-11-10T01:00:00Z')
        self.assertIsInstance(feedback, str)
        old = self.store.read(feedback)
        wiki = json.loads(json.dumps(old['wiki']))
        wiki['pages'][0]['content'] += '\nSynthetic reviewed revision: the warping lesson is withdrawn pending reconciliation of P-40.\n'
        change = dict(next(r for r in old['knowledge_records'] if r['kind']=='lesson'),
                      state='withdrawn', reason='Unreconciled negative application; not proof of falsehood')
        revised = revise_compilation(self.store, feedback, [change], wiki, old['extraction'],
            {'test_fixture':'Explicit compiled page revision; native Wiki compilation is exercised separately'},
            actor='Synthetic curator', method='Explicit reviewed revision', reason='Retain feedback and withdraw application',
            recorded_at='2026-11-10T01:02:00Z')
        self.assertIsInstance(revised, str)
        new = self.store.read(revised)
        self.assertEqual(len([r for r in new['knowledge_records'] if r['kind']=='outcome']), 1)
        self.assertNotEqual(new['manifest']['modules']['knowledge']['commit'], old['manifest']['modules']['knowledge']['commit'])
        self.assertIn('withdrawn pending', new['wiki']['pages'][0]['content'])
        self.assertNotIn('withdrawn pending', self.store.read(content)['wiki']['pages'][0]['content'])
        accepted = self.review(revised)
        self.assertEqual(next(r for r in records_for(self.store.read(accepted),accepted) if r['kind']=='lesson')['status'],'withdrawn')

    def test_source_correction_identifies_real_wiki_pages_for_review(self):
        from knowledge_atlas.core.feedback import revise_compilation
        old = self.store.read(self.base)
        wiki = json.loads(json.dumps(old['wiki']))
        source = next(s for s in wiki['sources'] if s['filename'] == 'review.txt')
        source['content'] += '\nSynthetic correction: profile Z takes approximately 47-63 minutes.\n'
        source['sha256'] = hashlib.sha256(source['content'].encode()).hexdigest()
        revised = revise_compilation(self.store, self.base, [], wiki, old['extraction'], {},
            actor='Synthetic curator', method='Retained compilation fixture', reason='Correct source duration',
            recorded_at='2026-11-10T01:02:00Z')
        proposal = json.loads(git(self.store.root, 'show', revised + ':proposal.json'))
        affected = proposal.get('wiki_impact', {})
        # Both pages in the current retained Wiki cite review.txt directly.
        expected_paths = ['/wiki/overview.md', '/wiki/concepts/conditional-warping.md']
        self.assertEqual(set(affected), {'Record/' + key('wiki', path) for path in expected_paths})
        source_record = 'Record/' + key('source', 'review.txt')
        for rid, detail in affected.items():
            self.assertEqual(detail['path'], [source_record, rid])
            self.assertEqual(detail['status'], 'needs_review')
            self.assertEqual(detail['content_change'], 'unchanged')
        self.assertEqual(self.store.read(revised)['wiki']['pages'], old['wiki']['pages'])
        self.assertEqual(self.store.read(self.base)['sources'], old['sources'])
        self.assertIsNone(Publications(self.store).ready())
        # Recompilation without a source change must not invent page impact.
        unchanged = revise_compilation(self.store, revised, [], wiki, old['extraction'], {},
            actor='Synthetic curator', method='Retained compilation fixture', reason='Repeat unchanged inputs',
            recorded_at='2026-11-10T01:03:00Z')
        self.assertEqual(json.loads(git(self.store.root, 'show', unchanged + ':proposal.json'))['wiki_impact'], {})


# Do not expose the imported helper class to unittest discovery a second time.
del KnowledgeTests
