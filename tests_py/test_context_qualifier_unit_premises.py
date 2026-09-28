import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_unit_premises_eval as v
from tests_py.test_context_qualifier_entity_refs_v2 import proposal as old_control


def proposal(case, entities):
    items = old_control(case, entities)[v.e.previous.VERSION]
    table = v.e.segments(case, entities)
    for item in items:
        del item['qualified']
        for limit in item['limitations']:
            limit['premiseUnits'] = list(dict.fromkeys(table[sid]['unitId'] for sid in limit.pop('premise')))
    return {v.e.VERSION: items}


class UnitPremisesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self, cid='gate:condition_paraphrase'):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        return c, copy.deepcopy(self.fixture['scenes'][c['sceneKey']])

    def test_all_controls_match_unchanged_gold_without_semantic_approval(self):
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            r = proposal(c, entities)
            original = copy.deepcopy(r)
            result = v.e.assess(r, c, entities)
            self.assertEqual(result['status'], 'matched', (c['id'], result))
            self.assertEqual(r, original)
            self.assertEqual(result['proposedResponse'], original)
            self.assertFalse(result['productionEnablement'])
            self.assertTrue(result['reviewRequired'])
            self.assertEqual(result['semanticStatus'], 'unverified')
            if c['id'] == 'gate:condition_paraphrase':
                item = result['decodedProposal'][v.e.previous.VERSION][0]
                self.assertEqual(item['limitations'][0]['premise'], ['P1-U1-S1', 'P1-U1-S2'])
                self.assertEqual(item['qualified'], ['P1-U1-S1', 'P1-U1-S2', 'P1-U2-S1'])

    def test_source_is_lossless_once_and_gold_is_not_injected(self):
        for original in self.fixture['cases']:
            c = copy.deepcopy(original)
            entities = self.fixture['scenes'][c['sceneKey']]
            for draft in (c['draft'], '  '+c['draft']+'\n\n  '+c['draft']+' \n'):
                c['draft'] = draft
                data = v.model_input(c, entities)
                text = ''.join(x if isinstance(x, str) else ''.join(x['segments'].values()) for x in data['source'])
                self.assertEqual(text, draft)
                self.assertEqual(data['entityRefs'], v.e.previous.model_input(c, entities)['entityRefs'])
                self.assertEqual(v.messages(c, entities), v.messages(dict(c, targets=[], selectionExpectations=[], id='secret'), entities))
                self.assertNotIn('scene:', json.dumps(data))

    def test_premise_is_required_whole_ordered_units_not_fragments_or_core(self):
        c, entities = self.case()
        for units in ([], ['P1-U1-S2'], ['missing'], ['P1-U2'], ['P1-U1', 'P1-U1'], ['P1-U2', 'P1-U1']):
            r = proposal(c, entities)
            r[v.e.VERSION][0]['limitations'][0]['premiseUnits'] = units
            self.assertEqual(v.e.inspect(r, c, entities)['status'], 'invalid_response', units)

    def test_cue_never_relocated_or_rewritten(self):
        c, entities = self.case()
        for cue in (dict(segmentId='P1-U1-S2', quote='只要', occurrence=0),
                    dict(segmentId='P1-U1-S1', quote='只要守门弟子点头', occurrence=0),
                    dict(segmentId='P1-U1-S1', quote='只要', occurrence=1),
                    dict(segmentId='P1-U2-S1', quote='伤者', occurrence=0)):
            r = proposal(c, entities)
            r[v.e.VERSION][0]['limitations'][0]['cue'] = cue
            result = v.e.inspect(r, c, entities)
            self.assertEqual(result['status'], 'invalid_response')
            self.assertEqual(result['proposedResponse'], r)

    def test_other_limitations_cannot_borrow_premise_units(self):
        c, entities = self.case('gate:both')
        r = proposal(c, entities)
        r[v.e.VERSION][0]['limitations'][1]['premiseUnits'] = ['P1-U1']
        self.assertEqual(v.e.inspect(r, c, entities)['status'], 'invalid_response')

    def test_union_does_not_fill_gaps_or_allow_independent_sentence_conditions(self):
        c, entities = self.case('gate:unrelated_condition')
        for units in (['P1-U1'], ['P1-U1', 'P1-U2']):
            r = proposal(c, entities)
            r[v.e.VERSION][0]['limitations'] = [dict(kind='condition',
                cue=dict(segmentId='P1-U1-S1', quote='如果', occurrence=0), premiseUnits=units)]
            result = v.e.assess(r, c, entities)
            if len(units) == 1:
                self.assertEqual(result['status'], 'invalid_response')
                self.assertIn('不自动填补', result['error'])
            else:
                self.assertEqual(result['status'], 'mismatched')
                self.assertTrue(result['scopeDiagnostics'])

    def test_wrong_position_or_omitted_limitation_still_fails_gold(self):
        c, entities = self.case('gate:both')
        for mode in ('side', 'modality', 'all'):
            r = proposal(c, entities)
            item = r[v.e.VERSION][0]
            if mode == 'side':
                item['position']['value'] = 'outside'
            elif mode == 'modality':
                item['limitations'].pop()
            else:
                item['limitations'] = []
            self.assertEqual(v.e.assess(r, c, entities)['status'], 'mismatched')

    def test_old_or_redundant_protocol_is_not_silently_upgraded(self):
        c, entities = self.case()
        r = proposal(c, entities)
        item = r[v.e.VERSION][0]
        item['qualified'] = ['P1-U2-S1']
        for bad in (r, old_control(c, entities), {}):
            self.assertEqual(v.e.inspect(bad, c, entities)['status'], 'invalid_response')
        report = json.loads(v.previous.OUTPUT.read_text())
        for c, row in zip(self.fixture['cases'], report['cases']):
            raw = json.loads(row['calls'][0]['content'])
            self.assertEqual(v.e.inspect(raw, c, self.fixture['scenes'][c['sceneKey']])['status'], 'invalid_response')
        self.assertEqual(v.previous.audit(), json.loads(v.previous.AUDIT.read_text()))

    def test_preflight_checks_prompt_and_limits_before_gateway(self):
        v.check_prompt_binding()
        with patch.object(v, 'PROMPT', v.previous.PROMPT):
            with self.assertRaises(ValueError):
                v.check_prompt_binding()
        altered = copy.deepcopy(self.fixture)
        altered['cases'][0]['draft'] = '伤者在封山线内。' * 1501
        with TemporaryDirectory() as d, patch.object(v, 'load_cases', return_value=altered), patch.object(v.f, '_gateway') as gateway:
            path = Path(d)/'report.json'
            with self.assertRaises(ValueError):
                v.run(path)
            gateway.assert_not_called()
            self.assertFalse(path.exists())

    def test_mock_run_once_audit_and_tamper_detection(self):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(proposal(c, self.fixture['scenes'][c['sceneKey']]))
            recorder.calls.append(dict(messages=messages, content=content,
                                       rawResponse=json.dumps({'choices': [{'message': {'content': content}}]})))
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        with TemporaryDirectory() as d, patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(v.f, '_gateway', return_value=recorder):
            path = Path(d)/'report.json'
            report = v.run(path)
            self.assertEqual(report['summary']['matched'], 18)
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                v.run(path)
            self.assertEqual(len(recorder.calls), 18)
            report['cases'][0]['calls'][0]['messages'][1]['content'] = '{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, '实际输入不一致'):
                v.audit(path)
