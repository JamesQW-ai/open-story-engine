import copy
import unittest

from test_support import context_qualifier_selection as s
from test_support import context_qualifier_scope_typed_eval as previous
from tests_py.test_context_qualifier_scope_v2 import proposal as old_control


def proposal(case, entities):
    """Explicit test controls; never used to convert live model responses."""
    source = old_control(case)
    units = s.f.indexed_input(case)['units']
    table = s.segments(case, entities)

    def span(ref):
        return s.v2.resolve(ref, units, list(units))

    def ids(refs):
        intervals = [span(r) for r in refs]
        return [sid for sid, part in table.items() if any(part['start'] < r['end'] and r['start'] < part['end'] for r in intervals)]

    def anchor(ref):
        r = span(ref)
        sid, part = next((i, p) for i, p in table.items() if p['start'] <= r['start'] < r['end'] <= p['end'])
        positions = [i for i in range(len(part['quote'])) if part['quote'].startswith(r['quote'], i)]
        return dict(segmentId=sid, quote=r['quote'], occurrence=positions.index(r['start']-part['start']))

    result = dict(schemaVersion=s.VERSION, items=[])
    for item in source['items']:
        a = item['anchors']
        result['items'].append(dict(status=item['status'], normal=item['normal'], reason=item['reason'],
                                   subject=anchor(a['subject']), boundary=anchor(a['boundary']),
                                   core=ids(a['positionCore']), qualified=ids(a['qualifiedPosition']),
                                   limitations=[dict(kind=l['kind'], cue=anchor(l['cue']), premise=ids(l['premise'])) for l in item['limitations']]))
    return result


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = previous.load_cases()

    def case(self, cid):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        return c, self.fixture['scenes'][c['sceneKey']]

    def test_all_controls_keep_exact_source_and_existing_labels(self):
        for case in self.fixture['cases']:
            entities = self.fixture['scenes'][case['sceneKey']]
            response = proposal(case, entities)
            result = s.assess(response, case, entities)
            self.assertEqual(result['status'], 'matched', (case['id'], result))
            self.assertEqual(response, result['proposedResponse'])
            self.assertFalse(result['productionEnablement'])
            self.assertTrue(result['reviewRequired'])
            self.assertEqual(result['semanticStatus'], 'unverified')
            for evidence in result['evidence']:
                for span in evidence['anchors']['positionCore']:
                    self.assertEqual(case['draft'][span['start']:span['end']], span['quote'])

    def test_previously_fabricated_core_is_no_longer_generated_text(self):
        for cid, cue in [('gate:possibility', '可能'), ('gate:possibility_paraphrase', '或许')]:
            c, e = self.case(cid)
            r = proposal(c, e)
            actual = s.inspect(r, c, e)
            self.assertIn(cue, actual['evidence'][0]['anchors']['positionCore'][0]['quote'])
            r['items'][0]['core'] = ['伤者在封山线内']
            self.assertEqual(s.inspect(r, c, e)['status'], 'invalid_response')

    def test_segments_partition_units_without_duplicating_text_or_reading_gold(self):
        for c in self.fixture['cases']:
            e = self.fixture['scenes'][c['sceneKey']]
            table = s.segments(c, e)
            for uid, unit in s.f.indexed_input(c)['units'].items():
                self.assertEqual(''.join(x['quote'] for x in table.values() if x['unitId'] == uid), unit['quote'])
            expected = s.model_input(c, e)
            self.assertEqual(set(expected), {'segments', 'entities'})
            changed = dict(c, targets=[], labelRationale='secret', id='secret')
            self.assertEqual(s.model_input(changed, e), expected)
            r = proposal(c, e)
            self.assertEqual(s.inspect(r, c, e), s.inspect(r, changed, e))

    def test_skip_reorder_duplicate_and_unknown_ids_are_rejected(self):
        c, e = self.case('gate:both')
        for ids in (['P1-U1-S1', 'P1-U2-S1'], ['P1-U2-S1','P1-U1-S1'], ['P1-U2-S1']*2, ['fake']):
            r = proposal(c, e)
            r['items'][0]['qualified'] = ids
            self.assertEqual(s.inspect(r, c, e)['status'], 'invalid_response', ids)

    def test_other_person_anchor_and_overbroad_core_are_not_accepted(self):
        c, e = self.case('hall:two_people')
        r = proposal(c, e)
        r['items'][1]['core'] = copy.deepcopy(r['items'][0]['core'])
        self.assertEqual(s.inspect(r, c, e)['status'], 'invalid_response')
        r = proposal(c, e)
        r['items'][0]['core'] = list(s.segments(c, e))
        r['items'][0]['qualified'] = list(s.segments(c, e))
        result = s.assess(r, c, e)
        self.assertEqual(result['status'], 'mismatched')
        self.assertIn('core_contains_multiple_person_mentions', [x['code'] for x in result['issues']])

    def test_unrelated_cue_and_cross_sentence_envelope_stay_rejected(self):
        c, e = self.case('hall:unrelated_uncertainty')
        r = proposal(c, e)
        item = r['items'][0]
        item.update(status='needs_review', normal=None)
        item['limitations'] = [dict(kind='modality', cue=dict(segmentId='P1-U1-S1',quote='可能',occurrence=0),premise=[])]
        self.assertEqual(s.inspect(r, c, e)['status'], 'invalid_response')
        item['qualified'] = list(s.segments(c,e))
        result = s.inspect(r,c,e)
        self.assertIn('qualified_position_crosses_sentence', [x['code'] for x in result['issues']])

    def test_condition_cannot_omit_premise_or_change_scope(self):
        c, e = self.case('gate:both')
        r = proposal(c,e)
        r['items'][0]['limitations'][0]['premise'] = []
        result = s.inspect(r,c,e)
        self.assertIn('condition_premise_missing', [x['code'] for x in result['issues']])
        r = proposal(c,e)
        r['items'][0]['limitations'][0]['scope'] = ['P1-U1-S1']
        self.assertEqual(s.inspect(r,c,e)['status'], 'invalid_response')

    def test_negation_and_outer_qualification_remain_distinct(self):
        c,e = self.case('hall:negative_position')
        result = s.inspect(proposal(c,e),c,e)
        self.assertIn('不在',result['evidence'][0]['anchors']['positionCore'][0]['quote'])
        self.assertEqual(result['projection']['items'][0]['normal']['polarity'],'negative')
        c,e = self.case('hall:denied_certainty')
        result = s.inspect(proposal(c,e),c,e)
        self.assertEqual(result['issues'],[])
        self.assertIsNone(result['projection']['items'][0]['normal'])
        self.assertNotIn('没有人敢断定',result['evidence'][0]['anchors']['positionCore'][0]['quote'])

    def test_precision_and_budget_are_conservative(self):
        c,e = self.case('gate:plain_emphasis')
        c['draft'] = '伤者' * 129
        with self.assertRaises(ValueError): s.model_input(c,e)
        self.assertEqual(s.person_occurrences('陌生伤者',e), [(0,4)])

    def test_legacy_payload_is_not_silently_upgraded(self):
        c,e = self.case('gate:possibility')
        self.assertEqual(s.inspect(old_control(c),c,e)['status'], 'invalid_response')
