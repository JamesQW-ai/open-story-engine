import copy
import unittest

from test_support import context_qualifier_selection_scope_audit as audit
from tests_py.test_context_qualifier_selection_policy import proposal


class SelectionScopeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = audit.experiment.load_cases()
        cls.case = next(c for c in fixture['cases'] if c['id'] == 'gate:both')
        cls.entities = fixture['scenes']['gate']

    def test_independent_premise_does_not_trigger_overlap(self):
        result = proposal(self.case, self.entities)
        issues = audit.diagnostics(result)
        self.assertNotIn('condition_premise_overlaps_core', [x['code'] for x in issues])

    def test_conclusion_in_premise_is_reported_without_changing_response(self):
        result = proposal(self.case, self.entities)
        item = result['items'][0]
        condition = next(x for x in item['limitations'] if x['kind'] == 'condition')
        condition['premise'] = list(item['qualified'])
        original = copy.deepcopy(result)
        issues = audit.diagnostics(result)
        self.assertIn('condition_premise_overlaps_core', [x['code'] for x in issues])
        self.assertEqual(result, original)

    def test_missing_interpretation_is_not_hidden_by_review_disposition(self):
        result = proposal(self.case, self.entities)
        result['items'][0]['position'] = None
        self.assertIn('position_interpretation_missing', [x['code'] for x in audit.diagnostics(result)])

    def test_live_report_keeps_original_failure_and_exposes_score_gaps(self):
        result = audit.audit()
        self.assertEqual(result['frozenSummary']['matched'], 16)
        self.assertEqual(result['summary'], dict(prior_failure=2, review_required=8,
                                                no_additional_issue_detected=8))
        self.assertEqual(result['newModelCalls'], 0)
        self.assertFalse(result['acceptance'])
        self.assertFalse(result['productionEnablement'])
