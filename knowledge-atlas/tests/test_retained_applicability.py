import unittest
from knowledge_atlas.core.applicability import assess, impact


class ApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.records = {
            'first': {'id': 'first', 'kind': 'episode', 'status': 'accepted', 'episode_id': 'trial-one', 'conditions': {'enclosure': 'closed'}},
            'retelling': {'id': 'retelling', 'kind': 'episode', 'status': 'accepted', 'episode_id': 'trial-one', 'conditions': {'enclosure': 'closed'}},
            'second': {'id': 'second', 'kind': 'episode', 'status': 'accepted', 'episode_id': 'trial-two', 'conditions': {'enclosure': 'closed'}},
            'dry': {'id': 'dry', 'kind': 'statement', 'status': 'accepted', 'conditions': {'feedstock': 'dried'}},
            'lesson': {'id': 'lesson', 'kind': 'lesson', 'status': 'accepted',
                'conditions': {'ambient_c': {'min': 20, 'max': 24}},
                'support_sets': [['first', 'dry'], ['retelling', 'dry'], ['second', 'dry']]},
            'wiki': {'id': 'wiki', 'kind': 'wiki', 'status': 'accepted', 'depends_on': ['lesson']},
        }
        self.context = {'enclosure': 'closed', 'feedstock': 'dried', 'ambient_c': 22}

    def test_unadopted_judgments_cannot_support_application_or_a_lesson(self):
        records = {'judgment': {'kind': 'judgment', 'status': 'accepted'},
                   'lesson': {'kind': 'lesson', 'status': 'accepted', 'support_sets': [['judgment']]}}
        for adoption in ('attributed_only', 'undetermined'):
            with self.subTest(adoption=adoption):
                records['judgment']['adoption'] = adoption
                self.assertEqual(assess(records, 'judgment', {})['status'], adoption)
                self.assertEqual(assess(records, 'lesson', {})['status'], 'insufficient')
        records['judgment']['adoption'] = 'adopted'
        self.assertEqual(assess(records, 'lesson', {})['status'], 'supported')

    def test_episode_inspection_does_not_count_retelling_as_independent(self):
        from knowledge_atlas.core import applicability
        records = {'observation': {'episode_id': 'trial-one', 'status': 'candidate'},
                   'retelling': {'episode_id': 'trial-one', 'status': 'candidate'},
                   'different': {'episode_id': 'trial-two', 'status': 'candidate'}}
        try:
            result = applicability.inspect_episodes(records, ['observation', 'retelling', 'different'])
        except AttributeError as error:
            self.fail('Episode independence inspection unavailable: '+str(error))
        self.assertIs(result['independent'], False)
        self.assertEqual(result['independence_basis'], 'same_episode')
        self.assertEqual(result['episodes'], ['trial-one', 'trial-two'])
        self.assertEqual(result['same_episode_records'], {'trial-one': ['observation', 'retelling']})
        self.assertEqual(records['observation']['status'], 'candidate')

    def test_episode_inspection_leaves_distinct_or_missing_evidence_unproven(self):
        from knowledge_atlas.core import applicability
        records = {'one': {'episode_id': 'trial-one'}, 'two': {'episode_id': 'trial-two'}, 'unknown': {}}
        try:
            distinct = applicability.inspect_episodes(records, ['one', 'two'])
            incomplete = applicability.inspect_episodes(records, ['one', 'one', 'unknown', 'absent'])
        except AttributeError as error:
            self.fail('Episode independence inspection unavailable: '+str(error))
        self.assertIsNone(distinct['independent'])
        self.assertEqual(distinct['independence_basis'], 'not_established')
        self.assertIsNone(incomplete['independent'])
        self.assertEqual(incomplete['same_episode_records'], {})
        self.assertEqual(incomplete['unresolved_records'], ['absent', 'unknown'])

    def test_conditions_are_checked_on_lesson_and_recursive_grounds(self):
        self.assertEqual(assess(self.records, 'lesson', self.context)['status'], 'supported')
        self.assertEqual(assess(self.records, 'lesson', {**self.context, 'ambient_c': 30})['status'], 'inapplicable')
        self.assertEqual(assess(self.records, 'lesson', {**self.context, 'enclosure': 'open'})['status'], 'insufficient')
        missing = assess(self.records, 'lesson', {'enclosure': 'closed', 'ambient_c': 22})
        self.assertEqual(missing['status'], 'conditional')
        self.assertIn('feedstock', missing['missing_conditions'])

    def test_conflicting_required_ground_cannot_justify_a_lesson(self):
        self.records['lesson']['support_sets']=[['first','dry']]
        self.records['first']['conflicts_with']=['alternative']
        self.records['alternative']={'id':'alternative','kind':'judgment','status':'accepted',
            'conditions':{'enclosure':'closed'},'conflicts_with':['first']}
        result=assess(self.records,'lesson',self.context)
        self.assertEqual(result['status'],'conflict')
        self.assertIn('alternative',result['conflicts'])
        self.assertIn('conflicting_ground',result['reasons'])
        self.records['alternative']['status']='withdrawn'
        self.assertEqual(assess(self.records,'lesson',self.context)['status'],'supported')

    def test_explicit_unknown_condition_remains_conditional_through_grounds(self):
        for unknown in (None, 'unknown'):
            with self.subTest(unknown=unknown):
                context = {**self.context, 'enclosure': unknown}
                direct = assess(self.records, 'first', context)
                nested = assess(self.records, 'lesson', context)
                self.assertEqual(direct['status'], 'conditional')
                self.assertEqual(nested['status'], 'conditional')
                self.assertIn('enclosure', nested['missing_conditions'])
        self.assertEqual(assess(self.records, 'first',
            {**self.context, 'enclosure': 'open'})['status'], 'inapplicable')

    def test_unknown_required_condition_cannot_establish_support(self):
        self.records['first']['conditions'] = {'enclosure': 'unknown'}
        result = assess(self.records, 'first', self.context)
        self.assertEqual(result['status'], 'conditional')
        self.assertEqual(result['missing_conditions'], ['enclosure'])

    def test_conflicted_or_branch_does_not_remove_independent_support(self):
        self.records['lesson']['support_sets']=[['first','dry'],['second','dry']]
        self.records['first']['conflicts_with']=['alternative']
        self.records['alternative']={'id':'alternative','kind':'judgment','status':'accepted',
            'conditions':{'enclosure':'closed'},'conflicts_with':['first']}
        result=assess(self.records,'lesson',self.context)
        self.assertEqual(result['status'],'supported')
        self.assertEqual(result['usable_support_sets'],[1])
        self.assertEqual(result['independent_episodes'],['trial-two'])
        self.records['alternative']['conditions']={'enclosure':'open'}
        self.assertEqual(assess(self.records,'lesson',self.context)['usable_support_sets'],[0,1])

    def test_opposing_position_uses_the_same_recursive_ground_checks(self):
        self.records['first']['conflicts_with']=['alternative']
        self.records['alternative']={'id':'alternative','kind':'judgment','status':'accepted',
            'support_sets':[['second']],'conflicts_with':['first']}
        self.records['second']['conflicts_with']=['counter']
        self.records['counter']={'id':'counter','kind':'judgment','status':'accepted',
                                'conflicts_with':['second']}
        self.assertEqual(assess(self.records,'alternative',self.context)['status'],'conflict')
        result=assess(self.records,'first',self.context)
        self.assertEqual(result['status'],'supported')
        self.assertEqual(result['conflicts'],[])

    def test_or_alternative_survives_withdrawal_and_retelling_is_not_independent(self):
        original = assess(self.records, 'lesson', self.context)
        self.assertEqual(original['independent_episodes'], ['trial-one', 'trial-two'])
        self.records['first']['status'] = 'withdrawn'
        self.records['retelling']['status'] = 'withdrawn'
        after = assess(self.records, 'lesson', self.context)
        self.assertEqual(after['status'], 'supported')
        self.assertEqual(after['independent_episodes'], ['trial-two'])
        self.assertEqual(after['usable_support_sets'], [2])

    def test_missing_joint_ground_is_insufficient_not_false_and_impacts_wiki(self):
        del self.records['dry']
        result = assess(self.records, 'lesson', self.context)
        self.assertEqual(result['status'], 'insufficient')
        self.assertEqual(result['usable_support_sets'], [])
        affected = impact(self.records, {'dry'})
        self.assertEqual(set(affected), {'lesson', 'wiki'})
        self.assertEqual(affected['wiki']['path'], ['dry', 'lesson', 'wiki'])
        self.assertEqual(affected['wiki']['status'], 'needs_review')

    def test_valid_time_is_distinct_from_acceptance_time_and_unknown_stays_unknown(self):
        record = self.records['first']
        record.update(valid_time={'from': '2026-02-01', 'to': '2026-04-01'}, accepted_at='2026-11-10T00:00:00Z')
        self.assertEqual(assess(self.records, 'first', self.context, valid_at='2026-03-01')['status'], 'supported')
        self.assertEqual(assess(self.records, 'first', self.context, valid_at='2026-04-01')['status'], 'inapplicable')
        result = assess(self.records, 'second', self.context, valid_at='2026-03-01')
        self.assertEqual(result['status'], 'conditional')
        self.assertIn('valid_time', result['missing_conditions'])

    def test_unreviewed_and_cyclic_grounds_do_not_become_supported(self):
        self.records['lesson']['support_sets'] = [['first']]
        self.records['first']['status'] = 'candidate'
        self.assertEqual(assess(self.records, 'lesson', self.context)['status'], 'needs_review')
        self.records['first']['status'] = 'accepted'
        self.records['first']['support_sets'] = [['lesson']]
        self.assertEqual(assess(self.records, 'lesson', self.context)['status'], 'insufficient')

    def test_recursive_unknown_and_failed_ground_reasons_remain_explainable(self):
        self.records['lesson']['support_sets'] = [['gap']]
        self.records['gap'] = {'kind': 'gap', 'status': 'accepted', 'missing_conditions': ['outcome']}
        result = assess(self.records, 'lesson', self.context)
        self.assertEqual(result['status'], 'insufficient')
        self.assertIn('outcome', result['missing_conditions'])
        self.assertIn('declared_gap', result['reasons'])
        self.records['gap']['status'] = 'withdrawn'
        self.assertIn('withdrawn_record', assess(self.records, 'lesson', self.context)['reasons'])
        del self.records['gap']
        self.assertIn('required_ground_unavailable', assess(self.records, 'lesson', self.context)['reasons'])
