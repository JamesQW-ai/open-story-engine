import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_frame_eval as v
from tests_py.test_context_qualifier_frame import control


class FrameEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self, cid='gate:both_paraphrase'):
        c = next(c for c in self.fixture['cases'] if c['id'] == cid)
        return c, self.fixture['scenes'][c['sceneKey']]

    def score(self, c, entities, response):
        return v.assess_content(json.dumps(response, ensure_ascii=False), c, entities)

    def audit(self, path):
        with patch.object(v, 'load_cases', return_value=copy.deepcopy(self.fixture)):
            return v.audit(path)

    def test_controls_have_explicit_denominators_and_scope_is_not_success(self):
        rows = []
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            result = self.score(c, entities, control(c, entities))
            expected = 'scope_pending' if c['cohort'] == 'scope_controls' else 'matched'
            self.assertEqual(result['status'], expected, (c['id'], result))
            self.assertFalse(result['productionEnablement'])
            rows.append(dict(calls=[], **result))
            hidden = dict(c, id='SECRET', cohort='SECRET', targets=[], selectionExpectations=[],
                          frameAttributions={}, coverageExpectation={})
            self.assertEqual(v.messages(c, entities), v.messages(hidden, entities))
        self.assertEqual((v.summary(rows)['matched'], v.summary(rows)['scope_pending']), (40, 4))
        metrics = v.metrics(rows)
        self.assertEqual(metrics['attribution']['expectedSubjects'], 48)
        self.assertEqual(metrics['positions']['expectedPositions'], 38)
        self.assertEqual(metrics['dependencies']['expectedConditionUnits'], 5)
        self.assertEqual(metrics['dependencies']['incorrectNonPremiseUnits'], 0)

    def test_explicit_wrong_non_premise_and_unresolved_are_not_hidden(self):
        c, entities = self.case()
        for field, status in (('nonPremiseUnits', 'mismatched'), ('unresolvedUnits', 'dependency_pending')):
            response = control(c, entities)
            item = response[v.frame.VERSION][0]['items'][0]
            item['conditions'] = []
            item[field] = ['P1-U1']
            result = self.score(c, entities, response)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['dependencyScore']['missingConditionUnits'], 1)
            if field == 'nonPremiseUnits':
                self.assertEqual(result['dependencyScore']['incorrectNonPremiseUnits'], 1)
                self.assertEqual(result['positionMetrics']['qualifierFalseNegative'], 1)
            else:
                self.assertEqual(result['dependencyScore']['unresolvedConditionUnits'], 1)
                for key in ('positionMetrics', 'reviewView', 'extractionAssessment', 'decodedProposal'):
                    self.assertNotIn(key, result)
                self.assertEqual(v.metrics([dict(calls=[], **result)])['positionScoredCases'], 0)
                self.assertIn('withheldProposal', result)

    def test_wrong_attribution_and_missing_positions_are_independent(self):
        c, entities = self.case('in_core:mention')
        for attribution in ('word_mention', 'unresolved'):
            response = control(c, entities)
            response[v.frame.VERSION][0].update(attribution=attribution, items=[])
            result = self.score(c, entities, response)
            self.assertEqual(result['status'], 'mismatched')
            self.assertEqual(result['positionMetrics']['missingPositions'], 1)
            self.assertEqual(result['attributionScore']['incorrectNonPosition'], int(attribution == 'word_mention'))
            self.assertEqual(result['attributionScore']['unexpectedUnresolved'], int(attribution == 'unresolved'))
        c, entities = self.case('control:object_position')
        response = control(c, entities)
        response[v.frame.VERSION][0]['attribution'] = 'word_mention'
        result = self.score(c, entities, response)
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(len(result['attributionScore']['wrongClassifications']), 1)
        self.assertEqual(result['positionMetrics']['extraPositions'], 0)

    def test_fabricated_position_counts_even_with_complete_tasks(self):
        c, entities = self.case('control:object_position')
        response = control(c, entities)
        subject, task = next(iter(v.frame.model_input(c, entities)['tasks'].items()))
        response[v.frame.VERSION][0].update(attribution='position', items=[dict(
            boundary=task['boundaries'][0], position=dict(value='inside', polarity='positive'),
            core=['P1-U1-S1'], conditions=[], modifiers=[], nonPremiseUnits=[], unresolvedUnits=[])])
        result = self.score(c, entities, response)
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(result['attributionScore']['unexpectedPosition'], 1)
        self.assertEqual(result['positionMetrics']['extraPositions'], 1)

    def test_invalid_attribution_lists_keep_errors_and_do_not_score_positions(self):
        c, entities = self.case('in_core:mention')
        for mode, key in (('missing', 'missingDecisions'), ('duplicate', 'duplicateDecisions'),
                          ('unknown', 'extraDecisions'), ('malformed', None)):
            response = control(c, entities)
            if mode == 'missing': response[v.frame.VERSION] = []
            elif mode == 'duplicate': response[v.frame.VERSION] *= 2
            elif mode == 'unknown': response[v.frame.VERSION][0]['subject'] = 'unknown'
            else: response[v.frame.VERSION][0]['attribution'] = []
            result = self.score(c, entities, response)
            self.assertEqual(result['status'], 'invalid_response', result)
            if key: self.assertEqual(result['attributionScore'][key], 1)
            self.assertNotIn('positionMetrics', result)
            self.assertNotIn('dependencyScore', result)

    def recorder(self, mode=None):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(control(c, self.fixture['scenes'][c['sceneKey']]))
            if mode == 'malformed' and not recorder.calls:
                content = 'not json'
            raw = dict(choices=[dict(message=dict(content=content))], usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            recorder.calls.append(dict(messages=messages, content=content, rawResponse=json.dumps(raw),
                                       observations=[dict(transport=dict(durationMs=20, httpStatus=200))]))
            if mode == 'transport':
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def execute(self, path, recorder):
        with patch.object(v, 'load_cases', return_value=copy.deepcopy(self.fixture)), \
                patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), \
                patch.object(v.f, '_gateway', return_value=recorder) as gateway:
            report = v.run(path)
            self.assertEqual(gateway.call_args.args[1], 44)
        return report

    def test_fixed_budget_audit_costs_exclusive_output_and_tampering(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            recorder = self.recorder()
            report = self.execute(path, recorder)
            self.assertEqual(report['summary']['matched'], 40)
            self.assertEqual(report['summary']['scope_pending'], 4)
            self.assertEqual(report['metrics']['costs']['tokens']['total_tokens'], 660)
            self.assertEqual(self.audit(path)['metrics'], report['metrics'])
            with self.assertRaises(FileExistsError):
                self.execute(path, recorder)
            self.assertEqual(len(recorder.calls), 44)
            for mode in ('score', 'raw', 'input', 'cost', 'hash'):
                changed = copy.deepcopy(report)
                if mode == 'score': changed['cases'][0]['attributionScore']['matched'] = False
                elif mode == 'raw': changed['cases'][0]['calls'][0]['content'] = '{}'
                elif mode == 'input': changed['cases'][0]['calls'][0]['messages'][1]['content'] = '{}'
                elif mode == 'cost': changed['metrics']['costs']['tokens']['total_tokens'] += 1
                else: changed['inputHashes'].pop(str(v.replay.FIXTURE.relative_to(v.f.ROOT)))
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): self.audit(path)

    def test_malformed_response_retained_and_not_retried(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('malformed'))
            self.assertEqual(report['actualCalls'], 44)
            self.assertEqual(report['summary']['invalid_response'], 1)
            self.assertEqual(self.audit(path)['summary'], report['summary'])

    def test_transport_failure_stops_remaining_calls(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('transport'))
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 43)
            self.assertEqual(len(report['cases'][0]['calls']), 1)
            with self.assertRaisesRegex(ValueError, '实验未完成'): self.audit(path)

    def test_raw_response_is_checkpointed_before_scoring_and_save_failure_stops(self):
        assess = v.assess_content
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            def checked(content, c, entities):
                row = json.loads(path.read_text())['cases'][-1]
                self.assertEqual(row['status'], 'incomplete')
                self.assertEqual(row['calls'][0]['content'], content)
                return assess(content, c, entities)
            with patch.object(v, 'assess_content', side_effect=checked):
                self.execute(path, self.recorder())
            original = v.f.save_checkpoint
            def fail_after_raw(path, report, **kwargs):
                if report['actualCalls']: raise OSError('disk full')
                return original(path, report, **kwargs)
            recorder = self.recorder()
            with patch.object(v.f, 'save_checkpoint', side_effect=fail_after_raw):
                with self.assertRaises(v.f.CheckpointError): self.execute(Path(d)/'failed.json', recorder)
            self.assertEqual(len(recorder.calls), 1)

    def test_fixture_or_prompt_drift_blocks_before_output_and_gateway(self):
        with TemporaryDirectory() as d:
            for field in ('FIXTURE', 'PROMPT'):
                bad = Path(d)/'bad'
                bad.write_text('{}')
                with patch.object(v.replay, field, bad), patch.object(v.f, '_gateway') as gateway:
                    path = Path(d)/('result-'+field)
                    with self.assertRaisesRegex(ValueError, '哈希变化'): v.run(path)
                    self.assertFalse(path.exists())
                    gateway.assert_not_called()
