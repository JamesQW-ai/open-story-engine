import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_semantic_scope_eval as v
from tests_py.test_context_qualifier_mention_contrast_eval import control as contrast_control
from tests_py.test_context_qualifier_unit_premises import proposal as regression_control


class SemanticScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def recorder(self, mode=None):
        recorder = SimpleNamespace(calls=[])

        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            control = contrast_control if c['cohort'] == 'contrast' else regression_control
            content = json.dumps(control(c, self.fixture['scenes'][c['sceneKey']]))
            if mode == 'malformed' and not recorder.calls:
                content = 'not json'
            recorder.calls.append(dict(messages=messages, content=content,
                rawResponse=json.dumps({'choices': [{'message': {'content': content}}]})))
            if mode == 'transport':
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)

        recorder.complete_json = complete
        return recorder

    def execute(self, path, recorder):
        with patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), \
                patch.object(v.f, '_gateway', return_value=recorder) as gateway:
            report = v.run(path)
            self.assertEqual(gateway.call_args.args[1], 30)
        return report

    def test_same_thirty_inputs_and_gold_isolation(self):
        self.assertEqual(len(self.fixture['cases']), 30)
        self.assertEqual(sum(c['cohort'] == 'contrast' for c in self.fixture['cases']), 12)
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            messages = v.messages(c, entities)
            self.assertEqual(messages[1], v.previous.messages(c, entities)[1])
            hidden = dict(c, id='hidden', cohort='hidden', pairId='hidden', variant='hidden',
                          targets=[], selectionExpectations=[])
            self.assertEqual(messages, v.messages(hidden, entities))
        self.assertIn('json', v.PROMPT.read_text().lower())
        self.assertIn(v.e.VERSION, v.PROMPT.read_text())

    def test_old_two_false_positives_are_not_repaired_by_scoring(self):
        report = json.loads(v.previous.OUTPUT.read_text())
        failures = []
        for c, r in zip(self.fixture['cases'][:12], report['cases']):
            result = v.assess_content(r['calls'][0]['content'], c, self.fixture['scenes']['hall'])
            self.assertEqual(result['status'], r['status'])
            if result['status'] != 'matched':
                failures.append(c['id'])
                self.assertTrue(result['proposedResponse'][v.e.VERSION][0]['limitations'])
                self.assertFalse(result['productionEnablement'])
        self.assertEqual(failures, ['prefix:mention', 'in_core:mention'])

    def test_mock_thirty_calls_audit_and_no_overwrite(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            recorder = self.recorder()
            report = self.execute(path, recorder)
            self.assertEqual(report['summary']['matched'], 30)
            self.assertEqual(report['cohorts']['contrast']['matched'], 12)
            self.assertEqual(report['cohorts']['regression']['matched'], 18)
            audit = v.audit(path)
            self.assertEqual(audit['summary'], report['summary'])
            self.assertEqual(audit['newModelCalls'], 0)
            self.assertFalse(audit['acceptance'])
            with self.assertRaises(FileExistsError):
                self.execute(path, recorder)
            self.assertEqual(len(recorder.calls), 30)

    def test_tamper_input_raw_score_binding_and_boundary_detected(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder())
            for mutation in ('input', 'raw', 'score', 'binding', 'boundary', 'cohort'):
                altered = copy.deepcopy(report)
                row = altered['cases'][0]
                if mutation == 'input':
                    row['calls'][0]['messages'][1]['content'] = '{}'
                elif mutation == 'raw':
                    row['calls'][0]['rawResponse'] = json.dumps({'choices': [{'message': {'content': '{}'}}]})
                elif mutation == 'score':
                    row['status'] = 'mismatched'
                elif mutation == 'binding':
                    altered['inputHashes'].pop(next(iter(altered['inputHashes'])))
                elif mutation == 'boundary':
                    altered['acceptance'] = True
                else:
                    row['cohort'] = 'regression'
                path.write_text(json.dumps(altered))
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    v.audit(path)

    def test_malformed_content_kept_as_failure_and_audited(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('malformed'))
            self.assertEqual(report['actualCalls'], 30)
            self.assertEqual(report['summary']['invalid_response'], 1)
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            self.assertEqual(report['cases'][0]['calls'][0]['content'], 'not json')

    def test_first_transport_failure_stops_without_retry(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('transport'))
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 29)
            with self.assertRaisesRegex(ValueError, '实验未完成'):
                v.audit(path)

    def test_prompt_drift_rejected_before_output_and_gateway(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            with patch.object(v, 'PROMPT', v.previous.PROMPT), patch.object(v.f, '_gateway') as gateway:
                with self.assertRaisesRegex(ValueError, '提示哈希'):
                    v.run(path)
                gateway.assert_not_called()
            self.assertFalse(path.exists())

    def test_checkpoint_failure_prevents_next_call(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            recorder = self.recorder()
            save = v.f.save_checkpoint

            def fail_after_raw(path, report, **kwargs):
                if report['actualCalls']:
                    raise OSError('disk unavailable')
                return save(path, report, **kwargs)

            with patch.object(v.f, 'save_checkpoint', side_effect=fail_after_raw):
                with self.assertRaises(v.f.CheckpointError):
                    self.execute(path, recorder)
            self.assertEqual(len(recorder.calls), 1)
