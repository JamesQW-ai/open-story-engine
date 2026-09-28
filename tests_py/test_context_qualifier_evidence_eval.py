import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_evidence_eval as v
from tests_py import test_context_qualifier_evidence as controls


class QualifierEvidenceEvalTests(unittest.TestCase):
    def recorder(self, fail=False):
        fixture = v.q.load_cases()
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            case = fixture['cases'][len(recorder.calls)]
            content = json.dumps(controls.proposal(case))
            recorder.calls.append(dict(messages=messages, content=content))
            if fail:
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_one_call_per_case_blind_input_and_reproducible_audit(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(v.f, '_gateway', return_value=recorder):
            path = Path(directory)/'report.json'
            report = v.run(path)
            self.assertEqual(report['actualCalls'], 9)
            self.assertEqual(report['summary']['matched'], 9)
            for call, case in zip(recorder.calls, v.q.load_cases()['cases']):
                payload = json.loads(call['messages'][1]['content'])
                self.assertEqual(set(payload), {'draft', 'units', 'entities'})
                self.assertEqual(payload['draft'], case['draft'])
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                v.run(path)
            self.assertEqual(len(recorder.calls), 9)
            report['cases'][0]['issues'].append({'code': 'forged'})
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, '评分不可复现'):
                v.audit(path)

    def test_transport_failure_stops_after_first_call(self):
        recorder = self.recorder(fail=True)
        with TemporaryDirectory() as directory, patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(v.f, '_gateway', return_value=recorder):
            report = v.run(Path(directory)/'report.json')
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 8)

    def test_save_failure_stops_after_first_call(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(v.f, '_gateway', return_value=recorder), patch.object(
                v.f, 'save_checkpoint', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(v.f.CheckpointError):
                v.run(Path(directory)/'report.json')
            self.assertEqual(len(recorder.calls), 1)
