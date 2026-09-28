import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_coverage_eval as v
from tests_py.test_context_qualifier_mention_contrast_eval import control as contrast_control
from tests_py.test_context_qualifier_unit_premises import proposal as regression_control


def control(c, entities):
    if c['cohort'] == 'regression':
        proposal = regression_control(c, entities)
    else:
        proposal = contrast_control(c, entities)
    for item, expected in zip(proposal[v.e.VERSION], c['selectionExpectations']):
        item['core'] = copy.deepcopy(min(expected['spans'], key=lambda s: len(s['core']))['core'])
    slots = v.coverage.model_input(c, entities)['coverageIndex']['slots']
    return {v.coverage.VERSION: [dict(slotId=sid, disposition=c['coverageExpectation']['decisions'][sid],
            items=[item for item in proposal[v.e.VERSION] if item['subject'] == slot['subjectRef']])
            for sid, slot in slots.items()]}


class CoverageEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self, cid='in_core:mention'):
        c = next(c for c in self.fixture['cases'] if c['id'] == cid)
        return c, self.fixture['scenes'][c['sceneKey']]

    def score(self, c, entities, response):
        return v.assess_content(json.dumps(response, ensure_ascii=False), c, entities)

    def test_frozen_controls_preserve_old_labels_and_separate_scope_pending(self):
        originals = v.previous.load_cases()['cases']
        for c, old in zip(self.fixture['cases'], originals):
            self.assertEqual({k: x for k, x in c.items() if k != 'coverageExpectation'}, old)
        statuses, positions, slots = [], 0, 0
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            result = self.score(c, entities, control(c, entities))
            wanted = 'scope_pending' if c['cohort'] == 'scope_controls' else 'matched'
            self.assertEqual(result['status'], wanted, (c['id'], result))
            self.assertFalse(result['productionEnablement'])
            self.assertEqual(result['coverage']['semanticCoverage'], 'unverified')
            if wanted == 'scope_pending':
                self.assertTrue(result['coverage']['pendingUnits'])
            statuses.append(wanted)
            positions += result['positionMetrics']['expectedPositions']
            slots += result['decisionScore']['expectedSlots']
        self.assertEqual((statuses.count('matched'), statuses.count('scope_pending'), positions, slots), (36, 2, 36, 40))

    def test_only_source_refs_and_tasks_go_to_model(self):
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            hidden = dict(c, id='SECRET', cohort='SECRET', coverageExpectation={}, targets=[], selectionExpectations=[])
            self.assertEqual(v.messages(c, entities), v.messages(hidden, entities))
            data = json.loads(v.messages(c, entities)[1]['content'])
            self.assertEqual(set(data), {'source', 'entityRefs', 'coverageTasks'})
            self.assertEqual(''.join(x if isinstance(x, str) else ''.join(x['segments'].values())
                                    for x in data['source']), c['draft'])
        self.assertIn('json', v.PROMPT.read_text().lower())

    def test_wrong_non_position_cannot_hide_a_missing_position(self):
        c, entities = self.case()
        response = control(c, entities)
        response[v.coverage.VERSION][0].update(disposition='non_position', items=[])
        result = self.score(c, entities, response)
        self.assertEqual(result['status'], 'mismatched')
        self.assertTrue(result['coverage']['decisionsComplete'])
        self.assertEqual(result['decisionScore']['incorrectNonPosition'], 1)
        self.assertEqual(result['positionMetrics']['missingPositions'], 1)
        self.assertEqual(result['proposedResponse'], response)

    def test_false_position_in_object_control_is_counted(self):
        c, entities = self.case('control:object_position')
        response = control(c, entities)
        sid, slot = next(iter(v.coverage.model_input(c, entities)['coverageIndex']['slots'].items()))
        response[v.coverage.VERSION][0].update(disposition='position', items=[dict(
            position=dict(value='inside', polarity='positive'), reason='wrong ownership inference',
            subject=slot['subjectRef'], boundary=slot['boundaryRefs'][0], core=['P1-U1-S1'], limitations=[])])
        result = self.score(c, entities, response)
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(result['positionMetrics']['extraPositions'], 1)
        self.assertEqual(result['decisionScore']['incorrectPosition'], 1)

    def test_unresolved_slot_remains_a_semantic_miss(self):
        c, entities = self.case()
        response = control(c, entities)
        response[v.coverage.VERSION][0].update(disposition='unresolved', items=[])
        result = self.score(c, entities, response)
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(len(result['coverage']['unresolvedSlots']), 1)
        self.assertEqual(result['positionMetrics']['missingPositions'], 1)
        self.assertEqual(result['decisionScore']['incorrectNonPosition'], 0)
        self.assertTrue(result['decisionScore']['wrongDispositions'])

    def test_missing_duplicate_and_unknown_decisions_are_visible(self):
        c, entities = self.case()
        base = control(c, entities)
        for mode, key in (('missing', 'missingDecisions'), ('duplicate', 'duplicateDecisions'), ('unknown', 'extraDecisions')):
            response = copy.deepcopy(base)
            if mode == 'missing': response[v.coverage.VERSION] = []
            elif mode == 'duplicate': response[v.coverage.VERSION] *= 2
            else: response[v.coverage.VERSION][0]['slotId'] = 'unknown'
            result = self.score(c, entities, response)
            self.assertEqual(result['status'], 'invalid_response')
            self.assertEqual(result['decisionScore'][key], 1)
            self.assertNotIn('positionMetrics', result)

    def test_qualifier_errors_separate_from_position_content(self):
        for cid, expected in (('in_core:mention', 'qualifierFalsePositive'),
                              ('in_core:operator', 'qualifierFalseNegative')):
            c, entities = self.case(cid)
            response = control(c, entities)
            item = response[v.coverage.VERSION][0]['items'][0]
            item['limitations'] = [] if cid.endswith('operator') else [dict(kind='modality',
                cue=dict(segmentId='P1-U1-S1', quote='可能', occurrence=0), premiseUnits=[])]
            result = self.score(c, entities, response)
            self.assertEqual(result['status'], 'mismatched')
            self.assertEqual(result['positionMetrics']['contentMatchedPositions'], 1)
            self.assertEqual(result['positionMetrics'][expected], 1)

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
        with patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), \
                patch.object(v.f, '_gateway', return_value=recorder) as gateway:
            report = v.run(path)
            self.assertEqual(gateway.call_args.args[1], 38)
        return report

    def test_fixed_budget_audit_costs_exclusive_output_and_tampering(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            recorder = self.recorder()
            report = self.execute(path, recorder)
            self.assertEqual(report['summary']['matched'], 36)
            self.assertEqual(report['summary']['scope_pending'], 2)
            self.assertEqual(report['metrics']['costs']['tokens']['total_tokens'], 570)
            self.assertEqual(v.audit(path)['metrics'], report['metrics'])
            with self.assertRaises(FileExistsError):
                self.execute(path, recorder)
            self.assertEqual(len(recorder.calls), 38)
            for mode in ('score', 'raw', 'input', 'cost', 'hash'):
                changed = copy.deepcopy(report)
                if mode == 'score': changed['cases'][0]['decisionScore']['matched'] = False
                elif mode == 'raw': changed['cases'][0]['calls'][0]['content'] = '{}'
                elif mode == 'input': changed['cases'][0]['calls'][0]['messages'][1]['content'] = '{}'
                elif mode == 'cost': changed['metrics']['costs']['tokens']['total_tokens'] += 1
                else: changed['inputHashes'].pop(str(v.FIXTURE.relative_to(v.f.ROOT)))
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): v.audit(path)

    def test_malformed_response_retained_and_not_retried(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('malformed'))
            self.assertEqual(report['actualCalls'], 38)
            self.assertEqual(report['summary']['invalid_response'], 1)
            self.assertEqual(v.audit(path)['summary'], report['summary'])

    def test_transport_failure_stops_remaining_calls(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('transport'))
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 37)
            self.assertEqual(len(report['cases'][0]['calls']), 1)
            with self.assertRaisesRegex(ValueError, '实验未完成'): v.audit(path)

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
                with patch.object(v, field, bad), patch.object(v.f, '_gateway') as gateway:
                    path = Path(d)/('result-'+field)
                    with self.assertRaisesRegex(ValueError, '哈希变化'): v.run(path)
                    self.assertFalse(path.exists())
                    gateway.assert_not_called()
