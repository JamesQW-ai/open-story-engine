import copy
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_selection_linear_eval as v
from tests_py import test_context_qualifier_selection as controls


class SelectionLinearEvalTests(unittest.TestCase):
    def recorder(self, fail=False):
        fixture = v.load_cases()
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            case = fixture['cases'][len(recorder.calls)]
            content = json.dumps(controls.proposal(case, fixture['scenes'][case['sceneKey']]))
            recorder.calls.append(dict(messages=messages, content=content, rawResponse=json.dumps({'choices':[{'message':{'content':content}}]})))
            if fail:
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_scene_inputs_stay_blind_and_manifest_cannot_change_budget(self):
        fixture = v.load_cases()
        self.assertEqual(sum(len(c['targets']) for c in fixture['cases']), 19)
        for case in fixture['cases']:
            entities = fixture['scenes'][case['sceneKey']]
            expected = v.messages(case, entities)
            self.assertEqual(set(json.loads(expected[1]['content'])), {'source','entities'})
            changed = copy.deepcopy(case)
            changed.update(targets=[], labelRationale='secret', id='secret')
            self.assertEqual(v.messages(changed, entities), expected)
        manifest = json.loads(v.FIXTURE.read_text())
        manifest['callLimit'] = 19
        with TemporaryDirectory() as directory:
            path = Path(directory)/'manifest.json'
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                v.load_cases(path)

    def test_eighteen_once_only_calls_audit_and_no_overwrite(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder):
            path = Path(directory)/'report.json'
            report = v.run(path)
            self.assertEqual(report['actualCalls'],18)
            self.assertEqual(report['summary']['matched'],18)
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                v.run(path)
            self.assertEqual(len(recorder.calls),18)
            original = copy.deepcopy(report)
            report['cases'][0]['calls'][0]['content']='{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'原始响应'):
                v.audit(path)
            report = original
            report['cases'][0]['calls'][0]['messages'][1]['content']='{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'实际输入'):
                v.audit(path)

    def test_transport_failure_stops_both_scenes(self):
        recorder = self.recorder(fail=True)
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder):
            report = v.run(Path(directory)/'report.json')
            self.assertEqual(report['actualCalls'],1)
            self.assertEqual(report['summary']['not_run'],17)

    def test_save_failure_stops_before_next_request(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder), patch.object(
                v.f,'save_checkpoint',side_effect=[None,OSError('disk full')]):
            with self.assertRaises(v.f.CheckpointError):
                v.run(Path(directory)/'report.json')
            self.assertEqual(len(recorder.calls),1)

    def test_linear_input_preserves_all_source_and_marks_every_segment_once(self):
        fixture = v.load_cases()
        for case in fixture['cases']:
            entities = fixture['scenes'][case['sceneKey']]
            source = v.model_input(case, entities)['source']
            self.assertEqual(re.sub(r'⟦[^⟧]+⟧', '', source), case['draft'])
            self.assertEqual(re.findall(r'⟦([^⟧]+)⟧', source), list(v.e.segments(case, entities)))
        case = copy.deepcopy(fixture['cases'][0])
        case['draft'] += '⟦P1-U1-S1⟧'
        with self.assertRaises(ValueError):
            v.model_input(case, entities)
