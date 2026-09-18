"""Current player-planner failure boundaries on every official long novel."""
import copy
import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from test_support.longform import ROOT, longform_cases
from open_story_engine.api_narrative import PlayerNarrativePlanner, apply_scene_repairs
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService, ReadError
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import MockPlanner
from open_story_engine.llm import Completion, LlmError
from open_story_engine.reader_scene_review import SceneReviewError


class RepairFailureGateway:
    model = 'longform-fault-fixture'

    def __init__(self, cid, action, body, empty_retry=False):
        self.body = body
        self.empty_retry = empty_retry
        self.calls = []
        self.contract = dict(decision='ready', readingIntent='brief',
            requirements={'A1': {'mode': 'result', 'summary': action}},
            method='留在当前地点', outcomes=[], goalUpdates=[],
            steps=[dict(id='S1', actorId=cid, action=action, requirementIds=['A1'], authority='player', causeStepId=None)],
            introductions=dict(characters=[], items=[], locations=[]), stateChanges=[],
            scenePlan=dict(start='当前场景', outcome='留在原地', stop='等待玩家决定',
                lengthReason='单次等待不改变地点', targetCjk=[80, 250],
                beats=[dict(purpose='回应等待行动', stepIds=['S1'])], knowledge=[], observationLimits=[]))
        self.authority = dict(decision='allow', issues=[],
            checks=[dict(stepId='S1', authorized=True, basis='player_input', quote=action, reason='直接授权')], stateChecks=[])

    def __deepcopy__(self, memo):
        # Single test request; retain the call ledger when PlayService clones planners.
        return self

    def complete_json(self, messages, *_args):
        payload = json.loads(messages[1]['content'])
        if 'problem' in payload and 'paragraphs' in payload:
            self.calls.append('repair')
            if self.empty_retry:
                return Completion('{"replacements": []}', '{"replacements": []}', [{'outcome': 'completed'}])
            error = LlmError('injected repair transport failure', 'transport_error')
            error.observations = [{'outcome': 'failed'}]
            raise error
        if 'steps' in payload and 'priorState' in payload:
            self.calls.append('authority')
            value = self.authority
        else:
            if self.calls:
                raise AssertionError('测试遇到未预期的模型调用')
            self.calls.append('plan')
            value = self.contract
        text = json.dumps(value, ensure_ascii=False)
        return Completion(text, text, [{'outcome': 'completed'}])

    def complete_text(self, messages, stream=None, reset=None):
        self.calls.append('prose')
        if self.empty_retry and self.calls.count('prose') == 2:
            return Completion('', '', [{'outcome': 'completed'}])
        if self.empty_retry and self.calls.count('prose') > 2:
            raise LlmError('injected later transport failure', 'transport_error')
        if stream:
            stream(self.body)
        return Completion(self.body, self.body, [{'outcome': 'completed'}], body_was_streamed=stream is not None)


class RepairAuditGateway(RepairFailureGateway):
    def __init__(self, cid, action, body, response):
        super().__init__(cid, action, body)
        self.response = response

    def complete_json(self, messages, *args):
        payload = json.loads(messages[1]['content'])
        if 'problem' in payload and 'paragraphs' in payload:
            self.calls.append('repair')
            return Completion(self.response, self.response, [{'outcome': 'completed'}])
        return super().complete_json(messages, *args)

    def complete_text(self, messages, stream=None, reset=None):
        if 'prose' in self.calls:
            raise LlmError('injected stop after rejected repair', 'model_output_rejected')
        return super().complete_text(messages, stream, reset)


class LongformRepairFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'STORY_PLANNER': 'mock'}))
        self.read = ReadService(ROOT / 'content/packages', Path(self.temp) / 'sessions.sqlite')
        self.play = PlayService(self.read, Path(self.temp))
        self.addCleanup(self.play.drafts.close)
        package = load_runtime_story_package(self.case['path'], lazy=True)
        self.cid = package['story']['entryModel']['sourceCharacterIds'][0]
        character = next(c for c in package['characters'] if c['id'] == self.cid)
        start = self.play.create_session(self.case['package_id'], self.case['version'], character['defaultEntryPointId'], self.cid, identity_opening=True)
        self.sid, self.parent = start['session']['id'], start['branch']['id']
        reader = json.loads((self.case['path'].parent / 'reader.json').read_text())
        self.paragraphs = [p.strip() for p in reader['chapters'][0]['text'].splitlines() if 60 <= len(p.strip()) <= 180][:3]
        self.assertEqual(len(self.paragraphs), 3)
        # An intentional identity-introduction error triggers the actual player-voice guard.
        self.body = '你是' + character['name'] + '。\n\n' + '\n\n'.join(self.paragraphs)
        self.action = '留在原地等待'

    def test_repair_transport_failure_retains_unconfirmed_body_and_audit_without_commit(self):
        gateway = RepairFailureGateway(self.cid, self.action, self.body)
        self.play._planner = PlayerNarrativePlanner(gateway)
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        events = []
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text=self.action, request_id='repair-failure',
                stream=lambda text: events.append(('delta', text)), stream_reset=lambda reason: events.append(('reset', reason)))
        self.assertEqual(gateway.calls, ['plan', 'authority', 'prose', 'repair', 'repair'])
        self.assertEqual(events[-2][0], 'reset')
        self.assertEqual(events[-1], ('delta', self.body))
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))
        with self.play.drafts.condition:
            self.assertTrue(self.play.drafts.condition.wait_for(lambda: sum(self.play.drafts.workers) == 0, timeout=5))
            job = next(iter(self.play.drafts.jobs.values()))
            self.assertEqual(job['status'], 'failed')
            self.assertNotIn('artifact', job)
            self.assertEqual(job['retained_draft'], {'text': self.body, 'status': 'unconfirmed'})
            audits = copy.deepcopy(job['failure_audits'])
        self.assertTrue(audits)
        records = [record for audit, _ in audits for record in audit['callObservations'] if record.get('generationStage') == 'local_repair_record']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['beforeBody'], self.body)
        self.assertIsNone(records[0]['afterBody'])
        self.assertEqual(records[0]['outcome'], 'failed')
        self.assertIn('式介绍', records[0]['issues'])
        self.assertIn('injected repair', records[0]['failureReason'])
        self.play.drafts.close()
        restored = PlayService(self.read, Path(self.temp))
        self.addCleanup(restored.drafts.close)
        with restored.drafts.condition:
            restored.drafts._initialize()
            recovered = next(iter(restored.drafts.jobs.values()))
        shown, resets = [], []
        with self.assertRaises(ReadError):
            restored.drafts.wait(recovered, shown.append, resets.append)
        self.assertEqual(shown, [self.body])
        self.assertEqual(resets, ['unconfirmed_draft'])
        self.assertEqual(recovered['failure_audits'], audits)

    def test_empty_retry_cannot_erase_last_readable_failed_draft(self):
        gateway = RepairFailureGateway(self.cid, self.action, self.body, empty_retry=True)
        self.play._planner = PlayerNarrativePlanner(gateway)
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        events = []
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text=self.action, request_id='empty-retry',
                stream=lambda text: events.append(('delta', text)), stream_reset=lambda reason: events.append(('reset', reason)))
        self.assertEqual(gateway.calls, ['plan', 'authority', 'prose', 'repair', 'prose', 'prose', 'prose'])
        job = next(iter(self.play.drafts.jobs.values()))
        self.assertEqual(job.get('retained_draft'), {'text': self.body, 'status': 'unconfirmed'})
        self.assertEqual(events[-1], ('delta', self.body))
        self.assertNotIn('artifact', job)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_player_repairs_reject_both_core_and_scene_placeholder_markers(self):
        body = '\n\n'.join(self.paragraphs)
        for marker in ('⟦REPAIR_GAP_1⟧', '[此处缺少事实依据：测试]', '[此处存在审查问题：测试]'):
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, '标记'):
                apply_scene_repairs(body, [{'paragraphId': 'P2', 'text': marker}])
        fixed = apply_scene_repairs(body, [{'paragraphId': 'P2', 'text': '你留在原地，暂时没有新的行动。'}])
        self.assertEqual(fixed, self.paragraphs[0] + '\n\n你留在原地，暂时没有新的行动。\n\n' + self.paragraphs[2])

    def failed_repair_records(self, gateway, request_id):
        self.play._planner = PlayerNarrativePlanner(gateway)
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        with self.assertRaises(ReadError):
            self.play.continue_turn(self.sid, self.parent, text=self.action, request_id=request_id)
        with self.play.drafts.condition:
            self.assertTrue(self.play.drafts.condition.wait_for(lambda: sum(self.play.drafts.workers) == 0, timeout=5))
            job = next(j for j in self.play.drafts.jobs.values() if j['binding'].get('request_id') == request_id)
            audits = copy.deepcopy(job['failure_audits'])
            self.assertEqual(job['status'], 'failed')
            self.assertNotIn('artifact', job)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))
        self.play.drafts.close()
        self.play = PlayService(self.read, Path(self.temp))
        self.addCleanup(self.play.drafts.close)
        with self.play.drafts.condition:
            self.play.drafts._initialize()
            restored = next(j for j in self.play.drafts.jobs.values() if j['binding'].get('request_id') == request_id)
        self.assertEqual(restored['failure_audits'], audits)
        return [r for audit, _ in audits for r in audit['callObservations'] if r.get('generationStage') == 'local_repair_record']

    def test_invalid_repair_audit_keeps_response_and_explicit_failure_stage(self):
        for index, response in enumerate(('not-json', '{"replacements": []}',
                json.dumps({'replacements': [{'paragraphId': 'P1', 'text': '⟦REPAIR_GAP_1⟧'}]}))):
            with self.subTest(response=response):
                gateway = RepairAuditGateway(self.cid, self.action, self.body, response)
                records = self.failed_repair_records(gateway, 'invalid-repair-' + str(index))
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record['beforeBody'], self.body)
                self.assertIsNone(record['afterBody'])
                self.assertEqual(record['repairResponse'], response)
                self.assertEqual(record['outcome'], 'rejected')
                self.assertEqual(record['failureStage'], 'repair_validation')
                self.assertTrue(record['failureCode'])
                self.assertEqual(record['issueTypes'], ['unknown'])

    def test_repaired_body_failing_review_has_new_problem_and_original_response(self):
        original = self.body.split('\n\n')[0]
        replacement = original + '你仍未移动。'
        response = json.dumps({'replacements': [{'paragraphId': 'P1', 'text': replacement}]}, ensure_ascii=False)
        records = self.failed_repair_records(RepairAuditGateway(self.cid, self.action, self.body, response), 'review-rejection')
        self.assertEqual(len(records), 2)
        first, second = records
        self.assertEqual(first['beforeBody'], self.body)
        self.assertEqual(first['afterBody'], self.body.replace(original, replacement, 1))
        self.assertEqual(first['repairResponse'], response)
        self.assertEqual(first['outcome'], 'failed_full_review')
        self.assertEqual(first['failureStage'], 'full_review')
        self.assertEqual(first['failureCode'], 'validation_error')
        self.assertIn('式介绍', first['failureIssues'])
        self.assertEqual(second['beforeBody'], first['afterBody'])
        self.assertEqual(second['outcome'], 'rejected')

    def test_typed_issues_and_review_transport_failure_remain_distinct(self):
        response = json.dumps({'replacements': [{'paragraphId': 'P1', 'text': '你留在原地。'}]}, ensure_ascii=False)
        issues = [dict(paragraphId='P1', type=kind, claim=self.body.split('\n\n')[0], reason=kind + '注入审查错误')
                  for kind in ('action', 'background', 'continuity', 'state')]
        timeout = LlmError('injected recheck timeout', 'transport_error')
        # Inject review failures, never successful verdicts; the real repair and store boundaries remain active.
        with patch('open_story_engine.api_narrative.check_player_voice', side_effect=[SceneReviewError(issues), timeout]):
            records = self.failed_repair_records(RepairAuditGateway(self.cid, self.action, self.body, response), 'review-transport')
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['issueTypes'], ['action', 'background', 'continuity', 'state'])
        self.assertEqual({i['type'] for i in record['issues']['issues']}, set(record['issueTypes']))
        self.assertIsNotNone(record['afterBody'])
        self.assertEqual(record['outcome'], 'failed_full_review')
        self.assertEqual(record['failureStage'], 'full_review')
        self.assertEqual(record['failureCode'], 'transport_error')
        self.assertIn('recheck timeout', record['failureReason'])

    def test_repair_request_transport_error_has_no_invented_after_body(self):
        record, = self.failed_repair_records(RepairFailureGateway(self.cid, self.action, self.body), 'repair-request-transport')
        self.assertEqual(record['failureStage'], 'repair_request')
        self.assertEqual(record['failureCode'], 'transport_error')
        self.assertIsNone(record['repairResponse'])
        self.assertIsNone(record['afterBody'])

    def test_new_review_issues_do_not_overwrite_original_problem_types(self):
        replacement = '你留在原地。'
        response = json.dumps({'replacements': [{'paragraphId': 'P1', 'text': replacement}]}, ensure_ascii=False)
        original = SceneReviewError([dict(paragraphId='P1', type='background',
            claim=self.body.split('\n\n')[0], reason='初审背景错误')])
        recheck = SceneReviewError([dict(paragraphId='P1', type='state', claim=replacement, reason='复核状态错误')])
        with patch('open_story_engine.api_narrative.check_player_voice', side_effect=[original, recheck]):
            records = self.failed_repair_records(RepairAuditGateway(self.cid, self.action, self.body, response), 'different-review-issues')
        self.assertEqual(records[0]['issueTypes'], ['background'])
        self.assertEqual(records[0]['failureIssueTypes'], ['state'])
        self.assertEqual(records[0]['issues']['issues'][0]['reason'], '初审背景错误')
        self.assertEqual(records[0]['failureIssues']['issues'][0]['reason'], '复核状态错误')
        self.assertEqual(records[1]['issueTypes'], ['state'])
        self.assertEqual(records[1]['beforeBody'], records[0]['afterBody'])

    def test_same_request_retry_preserves_each_failed_attempt_after_restart(self):
        request_id = 'audit-retry-history'
        prior = []
        for response in ('not-json', '{"replacements": []}', '{"replacements": false}'):
            with self.subTest(response=response):
                records = self.failed_repair_records(RepairAuditGateway(self.cid, self.action, self.body, response), request_id)
                job = next(j for j in self.play.drafts.jobs.values() if j['binding'].get('request_id') == request_id)
                history = job.get('previous_attempts', [])
                self.assertEqual(len(history), len(prior))
                for old, expected in zip(history, prior):
                    self.assertEqual(old['failure_audits'], expected)
                    self.assertEqual(old['status'], 'failed')
                    self.assertTrue(old['error']['code'])
                self.assertEqual(records[0]['repairResponse'], response)
                prior.append(copy.deepcopy(job['failure_audits']))
        self.play._planner = MockPlanner()
        # Keep this fixture's model identity; changing models correctly conflicts with a bound request.
        self.play._planner.gateway = SimpleNamespace(model=RepairFailureGateway.model)
        result = self.play.continue_turn(self.sid, self.parent, text=self.action, request_id=request_id)
        self.assertEqual(result['status'], 'written')
        job = next(j for j in self.play.drafts.jobs.values() if j['binding'].get('request_id') == request_id)
        self.assertEqual([old['failure_audits'] for old in job['previous_attempts']], prior)
        self.play.continue_turn(self.sid, self.parent, text=self.action, request_id='separate-request')
        separate = next(j for j in self.play.drafts.jobs.values() if j['binding'].get('request_id') == 'separate-request')
        self.assertEqual(separate['previous_attempts'], [])


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformRepair_' + case['package_id'], (LongformRepairFallbackTests,), {'case': case})))
    return suite
