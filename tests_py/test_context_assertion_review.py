import copy
import json
import unittest

from test_support import context_assertion_quote_eval as source
from test_support import context_assertion_review as review


class AssertionReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture, references, _ = source.prepare()
        cls.cases = {c['id']: c for c in fixture['cases']}
        cls.references = {r['caseId']: r['reference'] for r in references}
        cls.entities = fixture['scenes']

    def recorded(self, name, version=''):
        path = source.OUTPUT.with_name('context-assertion-quote' + version + '-eval-2026-09-27.json')
        return next(r['proposedResponse'] for r in json.loads(path.read_text())['cases'] if r['caseId'] == name)

    def evaluate(self, name, response):
        case = self.cases[name]
        before = copy.deepcopy(response)
        result = review.review(response, case, self.entities[case['sceneKey']], self.references[name])
        self.assertEqual(response, before)
        self.assertFalse(result['acceptance'])
        self.assertFalse(result['productionEnablement'])
        self.assertTrue(result['reviewRequired'])
        return result

    def test_recorded_terminal_punctuation_retains_old_failure(self):
        for name in ('two_people:first_operator', 'two_people:second_operator', 'hall:source_position'):
            result = self.evaluate(name, self.recorded(name))
            self.assertEqual(result['originalStatus'], 'reference_mismatch')
            self.assertEqual(result['category'], 'terminal_punctuation_only')

    def test_connector_is_not_punctuation(self):
        name = 'hall:two_people'
        self.assertEqual(self.evaluate(name, self.recorded(name))['category'], 'range_review_required')

    def test_negation_loss_is_not_normalized(self):
        name = 'denial:operator'
        result = self.evaluate(name, self.recorded(name, '-v2'))
        self.assertEqual(result['category'], 'semantic_review_required')
        self.assertIn('qualifiers', result['differingDimensions'])

    def test_reporter_cue_and_subject_binding_remain_pending(self):
        for name, category in (('frame:self_report', 'range_review_required'),
                               ('frame:other_report', 'binding_conflict')):
            self.assertEqual(self.evaluate(name, self.recorded(name, '-v2'))['category'], category)

    def test_item_multiplicity_is_not_erased(self):
        name = 'two_people:first_operator'
        response = self.recorded(name)
        items = response[review.contract.VERSION][0]['items']
        items.append(copy.deepcopy(items[0]))
        self.assertEqual(self.evaluate(name, response)['category'], 'semantic_review_required')

    def test_reordering_decisions_does_not_change_punctuation_finding(self):
        name = 'two_people:first_operator'
        response = self.recorded(name)
        response[review.contract.VERSION].reverse()
        self.assertEqual(self.evaluate(name, response)['category'], 'terminal_punctuation_only')

    def test_wrong_occurrence_is_invalid_not_normalized(self):
        name = 'two_people:first_operator'
        response = self.recorded(name)
        response[review.contract.VERSION][0]['items'][0]['core'][2] = 99
        self.assertEqual(self.evaluate(name, response)['category'], 'invalid_response')

    def test_matching_references_still_do_not_grant_authority(self):
        for name, response in self.references.items():
            result = self.evaluate(name, response)
            self.assertIn(result['category'], ('reference_match', 'out_of_scope'))

    def test_nested_abstention_difference_does_not_become_fact_leak(self):
        name = 'design:nested_pending'
        response = self.recorded(name, '-reasoning-capacity')
        self.assertEqual(self.evaluate(name, response)['category'], 'abstention_policy_review_required')
