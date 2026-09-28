import copy
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_explicit_segments_eval as v
from tests_py.test_context_qualifier_entity_refs_v2 import proposal


class ExplicitSegmentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == 'gate:condition_paraphrase'))
        return c, self.fixture['scenes'][c['sceneKey']]

    def assert_lossless(self, c, entities):
        data = v.model_input(c, entities)
        old = v.e.model_input(c, entities)
        self.assertEqual(data['entityRefs'], old['entityRefs'])
        self.assertEqual([b.replace('⟦/⟧', '') for b in data['sourceBlocks']], old['sourceBlocks'])
        source = ''.join(data['sourceBlocks'])
        self.assertEqual(re.sub(r'⟦[^⟧]+⟧', '', source), c['draft'])
        pairs = re.findall(r'⟦(P\d+-U\d+-S\d+)⟧(.*?)⟦/⟧', source, re.S)
        self.assertEqual(pairs, [(sid, part['quote']) for sid, part in v.e.segments(c, entities).items()])

    def test_all_fixed_sources_and_paragraph_gaps_are_lossless(self):
        for c in self.fixture['cases']:
            self.assert_lossless(c, self.fixture['scenes'][c['sceneKey']])
        c, entities = self.case()
        c['draft'] = '  只要守门弟子点头，伤者便在封山线外。\n\n “伤者在封山线内。”  \n'
        self.assert_lossless(c, entities)

    def test_prefix_is_visibly_separate_without_moving_it(self):
        c, entities = self.case()
        source = ''.join(v.model_input(c, entities)['sourceBlocks'])
        self.assertIn('⟦P1-U1-S1⟧只要⟦/⟧⟦P1-U1-S2⟧守门弟子点头，⟦/⟧', source)
        self.assertNotIn('只要', v.e.segments(c, entities)['P1-U1-S2']['quote'])

    def test_blind_input_and_bound_prompt(self):
        c, entities = self.case()
        self.assertEqual(v.messages(c, entities), v.messages(dict(c, targets=[], selectionExpectations=[], id='hidden'), entities))
        self.assertNotIn('scene:', json.dumps(v.model_input(c, entities)))
        v.check_prompt_binding()
        with patch.object(v, 'PROMPT', v.previous.PROMPT):
            with self.assertRaises(ValueError):
                v.check_prompt_binding()

    def test_collision_and_budget_fail_before_gateway_or_output(self):
        c, entities = self.case()
        for draft in ('伤者⟦/⟧在封山线内。', '伤者在封山线内。' * 1501):
            altered = copy.deepcopy(self.fixture)
            altered['cases'][0]['draft'] = draft
            with TemporaryDirectory() as d, patch.object(v, 'load_cases', return_value=altered), patch.object(v.f, '_gateway') as gateway:
                path = Path(d)/'report.json'
                with self.assertRaises(ValueError):
                    v.run(path)
                gateway.assert_not_called()
                self.assertFalse(path.exists())

    def test_saved_failure_is_not_repaired_or_regraded(self):
        c, entities = self.case()
        r = json.loads(v.previous.OUTPUT.read_text())
        raw = next(row for row in r['cases'] if row['caseId'] == c['id'])['proposedResponse']
        result = v.e.assess(raw, c, entities)
        self.assertEqual(result['status'], 'invalid_response')
        self.assertEqual(result['proposedResponse'], raw)
        audit = v.previous.audit()
        self.assertEqual(audit, json.loads(v.previous.AUDIT.read_text()))
        self.assertEqual(audit['summary']['matched'], 17)

    def test_correct_cue_does_not_hide_missing_condition_prefix(self):
        c, entities = self.case()
        control = proposal(c, entities)
        item = control[v.e.VERSION][0]
        # An independent negative control, never a rewrite of saved evidence.
        item['limitations'][0]['premise'] = ['P1-U1-S2']
        result = v.e.assess(control, c, entities)
        self.assertEqual(result['status'], 'mismatched')
        self.assertFalse(result['productionEnablement'])

    def test_mock_run_saves_actual_rendering_and_rejects_tampering(self):
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
