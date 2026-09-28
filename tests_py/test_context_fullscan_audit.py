import copy
import unittest
from test_support.context_fullscan_audit import alignment
from tests_py import test_context_fullscan as fixture_helpers


class FullscanAuditTests(unittest.TestCase):
    def setUp(self):
        # Use official longform fixtures without duplicating their test class.
        helper=fixture_helpers.FullscanTests()
        helper.setUpClass()
        self.helper=helper

    def test_extra_qualifier_is_not_a_missing_location(self):
        case=self.helper.case('conditional');items=self.helper.response(case)['items']
        items[0]['limitations'].append(dict(kind='modality',quote='伤者就在封山线内。'))
        result=alignment(items,case['targets'])
        self.assertEqual(result['locatedTargets'],1)
        self.assertEqual(result['missingLocations'],[])
        self.assertEqual(result['extraLocations'],[])
        self.assertEqual(result['semanticDifferences'][0]['extraLimitations'],['modality'])

    def test_shared_span_prefers_correct_pair_even_when_order_reversed(self):
        case=self.helper.case('two_subjects');items=list(reversed(self.helper.response(case)['items']))
        result=alignment(items,case['targets'])
        self.assertEqual(result['locatedTargets'],2);self.assertEqual(result['semanticDifferences'],[])

    def test_duplicate_at_same_span_does_not_mask_distinct_location(self):
        case=self.helper.case('repeated_mentions');items=self.helper.response(case)['items']
        items[1]=copy.deepcopy(items[0]);result=alignment(items,case['targets'])
        self.assertEqual(result['locatedTargets'],1)
        self.assertEqual(len(result['missingLocations']),1);self.assertEqual(len(result['extraLocations']),1)

    def test_wrong_normal_is_a_semantic_error_despite_matching_location(self):
        case=self.helper.case('original_v11');items=self.helper.response(case)['items']
        items[0]['normal']['value']='outside'
        result=alignment(items,case['targets'])
        self.assertEqual(result['locatedTargets'],1)
        self.assertEqual(result['semanticDifferences'][0]['fields'],['normal'])
