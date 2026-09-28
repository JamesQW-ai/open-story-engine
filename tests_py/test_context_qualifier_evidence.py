import copy
import unittest

from test_support import context_qualifier_eval as q
from test_support import context_qualifier_evidence as e


def ref(uid, quote, occurrence=0):
    return dict(unitId=uid, quote=quote, occurrence=occurrence)


def proposal(case):
    """Hand-labelled contract control; never used to manufacture live output."""
    target = case['targets'][0]
    units = e.f.indexed_input(case)['units']
    uid = target['unitIds'][-1]
    position = [ref(uid, units[uid]['quote'])]
    limits = []
    for kind in target['expectedLimitations']:
        if kind == 'condition':
            premise_id = target['unitIds'][0]
            quote = units[premise_id]['quote']
            cue = ref(premise_id, '如果' if quote.startswith('如果') else '只要')
            premise = [ref(premise_id, quote)]
        else:
            cue = ref(uid, '可能' if '可能' in units[uid]['quote'] else '或许')
            premise = []
        limits.append(dict(kind=kind, cue=cue, scope=copy.deepcopy(position), premise=premise))
    return dict(items=[dict(unitIds=list(target['unitIds']), status=target['expectedStatus'],
                            normal=copy.deepcopy(target['expectedNormal']), reason='contract control',
                            anchors=dict(subject=ref(uid, '伤者'), boundary=ref(uid, '封山线'), position=position),
                            limitations=limits)])


class QualifierEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = q.load_cases()

    def case(self, cid):
        return copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))

    def inspect(self, response, case):
        return e.inspect(response, case, self.fixture['entities'])

    def codes(self, result):
        return {i['code'] for i in result['issues']}

    def test_control_cases_are_structural_only_and_blind_to_gold(self):
        for case in self.fixture['cases']:
            response = proposal(case)
            result = self.inspect(response, case)
            self.assertEqual(result['status'], 'structurally_valid')
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertTrue(result['reviewRequired'])
            self.assertFalse(result['productionEnablement'])
            self.assertEqual(result['proposedResponse'], response)
            changed = copy.deepcopy(case)
            changed.update(targets=[], labelRationale='changed')
            self.assertEqual(result, self.inspect(response, changed))

    def test_program_resolves_unicode_and_repeated_occurrences(self):
        units = {'U1': dict(start=5, quote='雨中伤者看着伤者')}
        first = e.resolve(ref('U1', '伤者', 0), units, ['U1'])
        second = e.resolve(ref('U1', '伤者', 1), units, ['U1'])
        self.assertEqual((first['start'], first['end']), (7, 9))
        self.assertEqual((second['start'], second['end']), (11, 13))
        for r in (ref('U1', '伤者', True), ref('U1', '伤者', 2), ref('U1', '陌生人'), ref('U2', '伤者')):
            with self.assertRaises(ValueError):
                e.resolve(r, units, ['U1'])

    def test_condition_cannot_justify_extra_modality(self):
        case = self.case('original_condition')
        for quote in ('就', '伤者就在封山线内。'):
            response = proposal(case)
            item = response['items'][0]
            item['limitations'].append(dict(kind='modality', cue=ref('P1-U2', quote),
                                            scope=copy.deepcopy(item['anchors']['position']), premise=[]))
            before = copy.deepcopy(response)
            result = self.inspect(response, case)
            self.assertEqual(result['status'], 'evidence_issues')
            self.assertEqual(result['proposedResponse'], before)
            self.assertEqual(len(result['projection']['items'][0]['limitations']), 2)
            self.assertEqual(result['projection']['items'][0]['status'], 'needs_review')
            self.assertEqual(response, before)

    def test_real_coexisting_qualifiers_survive(self):
        case = self.case('both')
        result = self.inspect(proposal(case), case)
        self.assertEqual(result['issues'], [])
        item = result['projection']['items'][0]
        self.assertEqual([x['kind'] for x in item['limitations']], ['condition', 'modality'])
        self.assertEqual(item['status'], 'needs_review')

    def test_unrelated_cue_and_wrong_scope_reported(self):
        case = self.case('unrelated_possibility')
        response = proposal(case)
        item = response['items'][0]
        item['unitIds'].insert(0, 'P1-U1')
        item.update(status='needs_review', normal=None)
        item['limitations'] = [dict(kind='modality', cue=ref('P1-U1', '可能'),
                                   scope=copy.deepcopy(item['anchors']['position']), premise=[])]
        self.assertIn('modality_cue_outside_position', self.codes(self.inspect(response, case)))
        item['limitations'][0]['scope'] = [ref('P1-U1', '守门弟子可能会点头。')]
        self.assertIn('scope_does_not_match_position', self.codes(self.inspect(response, case)))

    def test_registered_subject_must_not_be_silently_relabelled(self):
        case = self.case('possibility')
        response = proposal(case)
        item = response['items'][0]
        item['limitations'].append(dict(kind='unresolved_subject', cue=ref('P1-U1', '伤者'),
                                       scope=copy.deepcopy(item['anchors']['position']), premise=[]))
        result = self.inspect(response, case)
        self.assertIn('registered_entity_marked_unresolved', self.codes(result))
        self.assertEqual(result['proposedResponse'], response)

    def test_negation_implicit_cue_and_homonym_never_gain_semantic_approval(self):
        for draft, cue in (('伤者不可能在封山线内。', '不可能'),
                           ('伤者想必在封山线内。', '想必'),
                           ('名牌上写着“可能”的伤者在封山线内。', '可能')):
            case = self.case('possibility')
            case['draft'] = draft
            response = proposal(case)
            item = response['items'][0]
            item['anchors']['position'] = [ref('P1-U1', draft)]
            item['limitations'] = [dict(kind='modality', cue=ref('P1-U1', cue),
                                        scope=copy.deepcopy(item['anchors']['position']), premise=[])]
            result = self.inspect(response, case)
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertTrue(result['reviewRequired'])
            self.assertEqual(result['proposedResponse'], response)
            self.assertFalse(result['productionEnablement'])

    def test_missing_premise_and_shared_cue_are_reported(self):
        case = self.case('both')
        response = proposal(case)
        limits = response['items'][0]['limitations']
        limits[0]['premise'] = []
        limits[1]['cue'] = copy.deepcopy(limits[0]['cue'])
        codes = self.codes(self.inspect(response, case))
        self.assertIn('condition_premise_missing', codes)
        self.assertIn('condition_and_modality_share_cue', codes)

    def test_candidate_anchor_mismatch_stays_unverified(self):
        case = self.case('plain_emphasis')
        response = proposal(case)
        response['items'][0]['normal']['subject'] = 'scene:gatekeeper'
        result = self.inspect(response, case)
        self.assertIn('candidate_entity_anchor_unverified', self.codes(result))
        self.assertEqual(result['projection']['items'][0]['normal']['subject'], 'scene:gatekeeper')

    def test_forged_or_missing_structural_fields_fail_closed(self):
        case = self.case('both')
        for mutation in ('missing', 'forged', 'duplicate', 'extra'):
            response = proposal(case)
            item = response['items'][0]
            if mutation == 'missing':
                del item['anchors']
            elif mutation == 'forged':
                item['limitations'][0]['cue']['quote'] = '不存在的词'
            elif mutation == 'duplicate':
                item['anchors']['position'] *= 2
            else:
                item['limitations'][0]['scope'][0]['start'] = 0
            result = self.inspect(response, case)
            self.assertEqual(result['status'], 'invalid_response')
            self.assertTrue(result['reviewRequired'])

    def test_missing_qualifier_is_not_hidden_by_valid_structure(self):
        case = self.case('both')
        response = proposal(case)
        response['items'][0]['limitations'].pop()
        score = e.assess(response, case, self.fixture['entities'])
        self.assertEqual(score['issues'], [])
        self.assertEqual(score['status'], 'mismatched')
        self.assertEqual(score['extractionScore']['missingTargets'], [0])
