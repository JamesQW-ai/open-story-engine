"""Frozen closing intent, cache invalidation and ending prerequisite boundaries."""
import copy
import json
import unittest
from unittest.mock import patch

from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError
from open_story_engine.api_turn_drafts import digest, visible_choices
from open_story_engine.cocreation import MockPlanner
from open_story_engine.llm import LlmError
from open_story_engine.reader_consequences import initial_goals, planning_context
from test_support.longform import longform_cases
from tests_api.test_longform_route_closure import LongformRouteClosureTests


class CapturePlanningGateway:
    model = 'closing-input-fixture'

    def __init__(self):
        self.messages = []

    def __deepcopy__(self, memo):
        return self

    def complete_json(self, messages, *_args):
        self.messages.append(copy.deepcopy(messages))
        raise LlmError('已捕获输入，测试不生成正文', 'model_output_rejected')

    def complete_text(self, *_args, **_kwargs):
        raise AssertionError('输入检查不应进入正文生成')


class LongformClosingPlanningTests(unittest.TestCase):
    setUp = LongformRouteClosureTests.setUp
    append = LongformRouteClosureTests.append
    lineage = LongformRouteClosureTests.lineage

    def plan(self, mode, bid=None):
        return self.play.plan_route_closure(self.sid, bid or self.parent, mode)

    def context(self, bid=None):
        _, snap = self.play._turn_snapshot(self.sid, bid or self.parent)
        return dict(package=snap.package, contract=snap.story_contract, parent=snap.history[-1],
                    lineage=snap.history, closingIntent=snap.frozen_closing_intent)

    def ready(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        binding = dict(binding, request_id='closing-draft')
        payload = {'text': '留在原地观察周围'}
        job = self.play._ensure_turn(binding, payload, snapshot, foreground=True)
        self.play.drafts.wait(job)
        self.play.drafts.release_selection(job)
        return binding, snapshot, payload, job, copy.deepcopy(job['artifact'])

    def test_actual_model_planning_input_receives_each_intent_without_ending_authority(self):
        for mode in ('normal', 'deviation', 'failure'):
            gateway = CapturePlanningGateway()
            self.play._planner = PlayerNarrativePlanner(gateway)
            self.plan(mode)
            with self.assertRaises(ReadError):
                self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='capture-' + mode)
            messages = gateway.messages[-1]
            payload = json.loads(messages[1]['content'])
            self.assertEqual(payload['closing']['intended_type'], mode)
            self.assertEqual(payload['closing']['phase'], 'preparing')
            self.assertTrue(payload['closure']['outstanding'])
            self.assertFalse(payload['closing']['ending_check']['approved'])
            self.assertIn('收束意图不代替本回合玩家行动授权', messages[0]['content'])
            self.assertIn('closing 阶段只能处理已有目标和剧情问题账本条目', messages[0]['content'])
            self.assertIn('证据不足时保持 unknown 或返回 clarification_needed', messages[0]['content'])
            self.assertEqual(len(gateway.messages), 1)
        with self.read.store() as store:
            self.assertEqual(len(store.branches(self.sid)), 1)

    def test_core_planner_receives_frozen_intent_and_saved_turn_keeps_facts_separate(self):
        self.plan('deviation')
        seen = []
        original = MockPlanner.plan
        def capture(planner, context, *args, **kwargs):
            seen.append(copy.deepcopy(context['closingIntent']))
            return original(planner, context, *args, **kwargs)
        with patch.object(MockPlanner, 'plan', capture):
            result = self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='frozen-intent')
        self.assertEqual(seen[0]['intended_type'], 'deviation')
        self.assertTrue(seen[0]['revision'])
        self.assertNotIn('closingIntent', result['branch']['branchState'])
        self.assertEqual(self.read.journey(self.sid, result['branch']['id'])['status'], 'active')

    def test_changed_intent_expires_ready_body_and_rejects_enqueue_and_commit(self):
        self.plan('normal')
        binding, snapshot, payload, job, artifact = self.ready()
        self.plan('failure')
        self.assertEqual(job['status'], 'expired')
        self.assertEqual(job['error']['code'], 'draft_expired')
        with self.assertRaises(ReadError) as error:
            self.play._ensure_turn(binding, payload, snapshot, foreground=True)
        self.assertEqual(error.exception.code, 'draft_expired')
        with self.assertRaises(ReadError) as error:
            self.play._commit_turn(job, artifact, payload, 'stale-intent')
        self.assertEqual(error.exception.code, 'draft_expired')
        with self.read.store() as store:
            self.assertIsNone(store.find_branch_request(self.sid, 'stale-intent'))

    def test_change_back_does_not_reactivate_old_revision(self):
        self.plan('normal')
        before, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        self.plan('deviation')
        self.plan('normal')
        after, _ = self.play._turn_snapshot(self.sid, self.parent)
        self.assertNotEqual(before['closing_digest'], after['closing_digest'])
        self.assertEqual(snapshot.frozen_closing_intent['intended_type'], 'normal')
        with self.assertRaises(ReadError):
            self.play._ensure_turn(before, {'text': '继续等待'}, snapshot)

    def test_identical_intent_preserves_ready_draft_and_restart_binding(self):
        self.plan('normal')
        binding, _, _, job, _ = self.ready()
        self.plan('normal')
        self.assertEqual(job['status'], 'ready')
        restored = PlayService(self.read, self.read.database_path.parent)
        self.addCleanup(restored.drafts.close)
        current, snapshot = restored._turn_snapshot(self.sid, self.parent)
        self.assertEqual(binding['closing_digest'], current['closing_digest'])
        self.assertEqual(snapshot.frozen_closing_intent['intended_type'], 'normal')

    def test_invalidation_keeps_a_job_already_bound_to_current_intent(self):
        self.plan('failure')
        binding, _, _, job, _ = self.ready()
        self.play.drafts.discard_parent(self.sid, self.parent, closing_digest=binding['closing_digest'])
        self.assertEqual(job['status'], 'ready')
        self.assertIn('artifact', job)

    def test_cancel_clears_planning_intent_and_replaces_binding(self):
        self.plan('failure')
        before, _ = self.play._turn_snapshot(self.sid, self.parent)
        self.plan(None)
        after, _ = self.play._turn_snapshot(self.sid, self.parent)
        self.assertNotEqual(before['closing_digest'], after['closing_digest'])
        self.assertIsNone(planning_context(self.context())['closing'])

    def test_another_service_cannot_commit_stale_intent_even_without_local_cancellation(self):
        self.plan('normal')
        _, _, payload, job, artifact = self.ready()
        other = PlayService(self.read, self.read.database_path.parent)
        self.addCleanup(other.drafts.close)
        other.plan_route_closure(self.sid, self.parent, 'deviation')
        with self.assertRaises(ReadError) as error:
            self.play._commit_turn(job, artifact, payload, 'cross-service-intent')
        self.assertEqual(error.exception.code, 'draft_expired')

    def test_prepare_rechecks_intent_when_snapshot_becomes_stale(self):
        old = self.play._turn_snapshot(self.sid, self.parent)
        self.plan('normal')
        with patch.object(self.play, '_turn_snapshot', return_value=old), \
                patch.object(self.play.drafts, 'ensure', side_effect=AssertionError('stale enqueue')):
            with self.assertRaises(ReadError) as error:
                self.play.prepare_choices(self.sid, self.parent, 'tab')
        self.assertEqual(error.exception.code, 'draft_expired')

    def test_old_menu_draft_id_is_rejected_before_generation(self):
        binding, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        choice = visible_choices(snapshot.history[-1], snapshot.package, snapshot.story_contract, snapshot.history)[0]
        old_id = digest(dict(binding, choice_id=choice['id']))
        self.plan('failure')
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('stale choice generated')):
            with self.assertRaises(ReadError) as error:
                self.play.continue_turn(self.sid, self.parent, choice_id=choice['id'], draft_id=old_id, request_id='old-menu')
        self.assertEqual(error.exception.code, 'draft_expired')

    def test_ending_types_check_goal_disposition_but_never_approve_outcome(self):
        threads = self.lineage()[-1]['branchState']['threadLedger']
        for status in ('completed', 'abandoned'):
            bid = self.append(goals=[dict(id=g['id'], title=g['title'], status=status)
                                     for g in initial_goals(self.package, self.contract)],
                              threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads])
            for mode in ('normal', 'deviation', 'failure'):
                self.plan(mode, bid)
                check = planning_context(self.context(bid))['closing']['ending_check']
                self.assertEqual(check['ledger_ready'], status == ('completed' if mode == 'normal' else 'abandoned'))
                self.assertFalse(check['approved'])
                self.assertTrue(check['outcome_review_required'])
                self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')

    def test_unknown_obligations_block_ending_prerequisites(self):
        bid = self.append(change=lambda s: s.update(goalLedger=[], threadLedger=[]))
        self.plan('normal', bid)
        closing = planning_context(self.context(bid))['closing']
        self.assertFalse(closing['ending_check']['ledger_ready'])
        self.assertIn('unresolved_obligations', [i['code'] for i in closing['ending_check']['unmet_conditions']])

    def test_sibling_intent_changes_do_not_invalidate_other_branch(self):
        self.plan('normal')
        left, right = self.append(), self.append()
        before, _ = self.play._turn_snapshot(self.sid, right)
        self.plan('failure', left)
        after, _ = self.play._turn_snapshot(self.sid, right)
        self.assertEqual(before, after)
        self.assertEqual(planning_context(self.context(left))['closing']['intended_type'], 'failure')
        self.assertEqual(planning_context(self.context(right))['closing']['intended_type'], 'normal')


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformClosingPlanning_' + case['package_id'],
                                                        (LongformClosingPlanningTests,), {'case': case})))
    return suite
