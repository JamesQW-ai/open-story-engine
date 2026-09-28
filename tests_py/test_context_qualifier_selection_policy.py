import copy
import unittest

from test_support import context_qualifier_selection_policy as s
from test_support import context_qualifier_scope_typed_eval as previous
from tests_py.test_context_qualifier_selection import proposal as selected_control


def proposal(case, entities):
    result = selected_control(case, entities)
    result['schemaVersion'] = s.VERSION
    for item in result['items']:
        item['position'] = item.pop('normal')
        del item['status']
    return result


class SelectionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture = previous.load_cases()

    def case(self, cid):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id']==cid))
        return c, self.fixture['scenes'][c['sceneKey']]

    def test_controls_keep_labels_without_granting_semantic_authority(self):
        for c in self.fixture['cases']:
            e=self.fixture['scenes'][c['sceneKey']]
            r=proposal(c,e)
            result=s.assess(r,c,e)
            self.assertEqual(result['status'],'matched',c['id'])
            self.assertEqual(result['proposedResponse'],r)
            self.assertTrue(result['reviewRequired'])
            self.assertFalse(result['productionEnablement'])
            self.assertEqual(result['semanticStatus'],'unverified')

    def test_every_limitation_blocks_unconditional_normal_even_with_position(self):
        c,e=self.case('gate:possibility')
        plain,_=self.case('gate:plain_emphasis')
        for kind in s.f.LIMITATIONS:
            r=proposal(c,e)
            r['items'][0]['position']=proposal(plain,e)['items'][0]['position']
            r['items'][0]['limitations'][0]['kind']=kind
            result=s.inspect(r,c,e)
            self.assertEqual(result['decodedSelection']['items'][0]['status'],'needs_review')
            self.assertIsNone(result['decodedSelection']['items'][0]['normal'])
            self.assertEqual(result['proposedResponse'],r)

    def test_model_status_legacy_payload_and_missing_position_are_rejected(self):
        c,e=self.case('gate:plain_emphasis')
        r=proposal(c,e);r['items'][0]['status']='candidate'
        self.assertEqual(s.inspect(r,c,e)['status'],'invalid_response')
        self.assertEqual(s.inspect(selected_control(c,e),c,e)['status'],'invalid_response')
        r=proposal(c,e);r['items'][0]['position']=None
        self.assertEqual(s.inspect(r,c,e)['status'],'invalid_response')

    def test_invalid_or_mismatched_position_is_not_hidden_by_review_state(self):
        c,e=self.case('gate:possibility')
        plain,_=self.case('gate:plain_emphasis')
        r=proposal(c,e);r['items'][0]['position']=proposal(plain,e)['items'][0]['position']
        r['items'][0]['position']['value']='invented'
        self.assertEqual(s.inspect(r,c,e)['status'],'invalid_response')
        r['items'][0]['position']['value']='inside'
        r['items'][0]['position']['subject']='scene:gatekeeper'
        result=s.inspect(r,c,e)
        self.assertIn('position_entity_anchor_mismatch',[x['code'] for x in result['issues']])

    def test_missing_condition_is_not_repaired_or_scored_as_pass(self):
        c,e=self.case('gate:both')
        r=proposal(c,e)
        r['items'][0]['limitations']=[x for x in r['items'][0]['limitations'] if x['kind']!='condition']
        result=s.assess(r,c,e)
        self.assertEqual(result['status'],'mismatched')
        self.assertEqual(result['proposedResponse'],r)

    def test_position_identity_cannot_fill_missing_subject_reference(self):
        c,e=self.case('gate:plain_emphasis')
        r=proposal(c,e)
        del r['items'][0]['subject']
        result=s.inspect(r,c,e)
        self.assertEqual(result['status'],'invalid_response')
        self.assertEqual(result['proposedResponse'],r)

    def test_unrelated_previous_condition_is_not_a_valid_envelope(self):
        c,e=self.case('gate:unrelated_condition')
        r=proposal(c,e)
        item=r['items'][0]
        item['qualified']=list(s.segments(c,e))
        item['limitations']=[dict(kind='condition',cue=dict(segmentId='P1-U1-S1',quote='如果',occurrence=0),premise=['P1-U1-S1'])]
        result=s.assess(r,c,e)
        self.assertEqual(result['status'],'mismatched')
        self.assertIn('qualified_position_crosses_sentence',[x['code'] for x in result['issues']])
        self.assertEqual(result['proposedResponse'],r)
