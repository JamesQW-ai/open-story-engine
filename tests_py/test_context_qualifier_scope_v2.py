import copy
import unittest

from test_support import context_qualifier_scope_eval as v
from tests_py import test_context_qualifier_evidence as gate
from tests_py import test_context_qualifier_heldout as hall


def proposal(case):
    """Construct v2 controls explicitly; never upgrade saved model evidence."""
    original = copy.deepcopy(case)
    original['id'] = case['id'].split(':', 1)[1]
    response = gate.proposal(original) if case['sceneKey'] == 'gate' else hall.response(original)
    response['schemaVersion'] = 'qualifier-scope/0.2'
    for item in response['items']:
        anchors = item['anchors']
        core = anchors.pop('position')
        anchors['positionCore'] = core
        premises = [r for limit in item['limitations'] for r in limit['premise']]
        anchors['qualifiedPosition'] = copy.deepcopy(premises + core)
        if original['id'] == 'denied_certainty':
            core[0]['quote'] = '沈砚秋在议事殿内'
            item['limitations'][0]['scope'] = copy.deepcopy(core)
    return response


class ScopeV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self, cid):
        return copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))

    def inspect(self, response, case):
        return v.e.inspect(response, case, self.fixture['scenes'][case['sceneKey']])

    def codes(self, result):
        return {x['code'] for x in result['issues']}

    def test_existing_semantics_match_without_granting_authority(self):
        for case in self.fixture['cases']:
            response = proposal(case)
            result = v.e.assess(response, case, self.fixture['scenes'][case['sceneKey']])
            self.assertEqual(result['status'], 'matched', case['id'])
            self.assertEqual(result['proposedResponse'], response)
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertTrue(result['reviewRequired'])
            self.assertFalse(result['productionEnablement'])

    def test_outer_uncertainty_can_wrap_position_core(self):
        case = self.case('hall:denied_certainty')
        result = self.inspect(proposal(case), case)
        self.assertEqual(result['issues'], [])
        evidence = result['evidence'][0]
        self.assertLess(evidence['limitations'][0]['cue']['end'], evidence['anchors']['positionCore'][0]['end'])
        self.assertLess(evidence['limitations'][0]['cue']['start'], evidence['anchors']['positionCore'][0]['start'])
        self.assertEqual(result['projection']['items'][0]['status'], 'needs_review')
        self.assertIsNone(result['projection']['items'][0]['normal'])

    def test_candidate_core_cannot_omit_subject_even_with_complete_envelope(self):
        case = self.case('hall:negative_position')
        response = proposal(case)
        item = response['items'][0]
        item['anchors']['positionCore'] = [gate.ref('P1-U1', '不在议事殿内')]
        result = self.inspect(response, case)
        self.assertIn('candidate_entity_outside_position', self.codes(result))
        self.assertEqual(result['proposedResponse'], response)
        self.assertFalse(result['productionEnablement'])
        item['anchors']['positionCore'] = [gate.ref('P1-U1', '沈砚秋不在议事殿内')]
        self.assertEqual(self.inspect(response, case)['issues'], [])

    def test_borrowed_cue_remains_a_problem_even_if_envelope_expanded(self):
        case = self.case('hall:unrelated_uncertainty')
        response = proposal(case)
        item = response['items'][0]
        item['unitIds'].insert(0, 'P1-U1')
        item.update(status='needs_review', normal=None)
        item['limitations'] = [dict(kind='modality', cue=gate.ref('P1-U1','可能'),
                                   scope=copy.deepcopy(item['anchors']['positionCore']), premise=[])]
        self.assertIn('cue_outside_qualified_position', self.codes(self.inspect(response, case)))
        item['anchors']['qualifiedPosition'].insert(0, gate.ref('P1-U1','开门长老可能会开口。'))
        self.assertIn('qualified_position_crosses_sentence', self.codes(self.inspect(response, case)))

    def test_cannot_skip_intervening_text_to_join_cue_and_core(self):
        case = self.case('hall:unrelated_uncertainty')
        response = proposal(case)
        item = response['items'][0]
        item['unitIds'].insert(0, 'P1-U1')
        item['anchors']['qualifiedPosition'].insert(0, gate.ref('P1-U1','可能'))
        self.assertIn('qualified_position_skips_text', self.codes(self.inspect(response, case)))

    def test_core_and_condition_premise_must_be_in_complete_proposition(self):
        case = self.case('gate:both')
        response = proposal(case)
        item = response['items'][0]
        item['anchors']['qualifiedPosition'] = copy.deepcopy(item['anchors']['positionCore'])
        codes = self.codes(self.inspect(response, case))
        self.assertIn('premise_outside_qualified_position', codes)
        self.assertIn('cue_outside_qualified_position', codes)
        item['anchors']['qualifiedPosition'] = copy.deepcopy(item['limitations'][0]['premise'])
        self.assertIn('core_outside_qualified_position', self.codes(self.inspect(response, case)))

    def test_scope_cannot_switch_to_other_subject(self):
        case = self.case('hall:two_people')
        response = proposal(case)
        first, second = response['items']
        second.update(status='needs_review', normal=None)
        second['limitations'] = [dict(kind='ambiguity', cue=copy.deepcopy(second['anchors']['subject']),
                                     scope=copy.deepcopy(first['anchors']['positionCore']), premise=[])]
        self.assertIn('scope_does_not_match_position', self.codes(self.inspect(response, case)))

    def test_position_negation_is_distinct_from_negated_certainty(self):
        case = self.case('hall:negative_position')
        result = self.inspect(proposal(case), case)
        self.assertEqual(result['projection']['items'][0]['normal']['polarity'], 'negative')
        case = self.case('hall:denied_certainty')
        response = proposal(case)
        response['items'][0].update(status='candidate', normal=copy.deepcopy(result['projection']['items'][0]['normal']), limitations=[])
        score = v.e.assess(response, case, self.fixture['scenes']['hall'])
        self.assertEqual(score['status'], 'mismatched')

    def test_labels_are_not_dropped_to_make_structure_pass(self):
        case = self.case('gate:original_condition')
        response = proposal(case)
        item = response['items'][0]
        item['limitations'].append(dict(kind='modality', cue=gate.ref('P1-U2','就'),
                                       scope=copy.deepcopy(item['anchors']['positionCore']), premise=[]))
        result = self.inspect(response, case)
        self.assertIn('connective_alone_not_epistemic_evidence', self.codes(result))
        self.assertEqual(result['proposedResponse'], response)
        self.assertEqual(len(result['projection']['items'][0]['limitations']), 2)

    def test_v1_or_missing_complete_proposition_is_not_silently_upgraded(self):
        case = self.case('hall:denied_certainty')
        for mutation in ('version','missing','forged'):
            response = proposal(case)
            if mutation == 'version':
                del response['schemaVersion']
            elif mutation == 'missing':
                del response['items'][0]['anchors']['qualifiedPosition']
            else:
                response['items'][0]['anchors']['qualifiedPosition'][0]['quote'] = 'fake'
            self.assertEqual(self.inspect(response, case)['status'], 'invalid_response')

    def test_gold_does_not_enter_structure_inspection(self):
        case = self.case('hall:denied_certainty')
        response = proposal(case)
        expected = self.inspect(response, case)
        case['targets'] = []
        self.assertEqual(self.inspect(response, case), expected)

    def test_single_reference_fields_are_not_unwrapped_from_arrays(self):
        case = self.case('gate:both')
        for field in ('subject', 'boundary', 'cue'):
            response = proposal(case)
            item = response['items'][0]
            owner = item['limitations'][0] if field == 'cue' else item['anchors']
            owner[field] = [owner[field]]
            result = self.inspect(response, case)
            self.assertEqual(result['status'], 'invalid_response', field)
            self.assertEqual(result['proposedResponse'], response)

    def test_core_must_not_remove_modality_to_invent_a_quote(self):
        case = self.case('gate:possibility_paraphrase')
        response = proposal(case)
        item = response['items'][0]
        item['anchors']['positionCore'] = [gate.ref('P1-U1', '伤者在封山线外')]
        result = self.inspect(response, case)
        self.assertEqual(result['status'], 'invalid_response')
        self.assertEqual(result['proposedResponse'], response)
