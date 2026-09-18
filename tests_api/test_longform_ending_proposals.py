"""Ending transaction contracts on the official corpus, not prose acceptance."""
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadError
from open_story_engine.llm import Completion, LlmError
from open_story_engine.reader_consequences import initial_goals
from open_story_engine.route_endings import CHECKS
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_route_closure import LongformRouteClosureTests
from tests_api.test_longform_route_outline import LongformRouteOutlineTests


class EndingGateway:
    model = 'ending-review-fixture'

    def __init__(self, decision='allow', mutate=None, on_call=None, error=None):
        self.decision, self.mutate, self.on_call, self.error = decision, mutate, on_call, error
        self.inputs = []

    def complete_json(self, messages):
        payload = json.loads(messages[1]['content'])
        self.inputs.append(payload)
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        value = dict(decision=self.decision, checks={key: dict(passed=self.decision == 'allow',
                     evidence=payload['ending_quote'], reason='仅测试审查契约与提交隔离') for key in CHECKS})
        if self.mutate:
            self.mutate(value)
        return Completion(json.dumps(value, ensure_ascii=False), json.dumps({'review': value, 'usage': {'total_tokens': 17}}), [])


class LongformEndingProposalTests(unittest.TestCase):
    setUp = LongformRouteClosureTests.setUp
    append = LongformRouteClosureTests.append
    lineage = LongformRouteClosureTests.lineage
    rewrite = LongformRouteOutlineTests.rewrite

    def ready(self, mode='normal'):
        threads = self.lineage()[-1]['branchState']['threadLedger']
        body = '你确认这一段追查已经有了交代，收起手边的记录，在此停下。'
        bid = self.append(goals=[dict(id=g['id'], title=g['title'], status='completed' if mode == 'normal' else 'abandoned')
                                 for g in initial_goals(self.package, self.contract)],
                          threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads], evidence=body)
        self.play.plan_route_closure(self.sid, bid, mode)
        return bid, body

    def gateway(self, **kwargs):
        gateway = EndingGateway(**kwargs)
        self.play._planner = SimpleNamespace(gateway=gateway)
        return gateway

    def propose(self, bid, body, request='ending-1'):
        return self.play.propose_ending(self.sid, bid, request, '这一段路线在此收尾', body)

    def test_all_types_require_explicit_commit_then_block_continuation(self):
        for mode in ('normal', 'deviation', 'failure'):
            bid, body = self.ready(mode)
            gateway = self.gateway()
            proposal = self.propose(bid, body)
            self.assertEqual(proposal['status'], 'approved')
            self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')
            self.assertEqual(proposal['audit']['metrics']['reported_tokens'], 17)
            result = self.play.commit_ending(self.sid, bid, proposal['id'])
            self.assertEqual(result['receipt']['ending_type'], mode)
            self.assertTrue(result['receipt']['ending_written'])
            view = self.read.route_closure(self.sid, bid)
            self.assertTrue(view['ending_written'])
            self.assertEqual(view['lifecycle']['phase'], 'ended')
            self.assertTrue(self.read.journey(self.sid, bid)['route_health']['ending_written'])
            with self.assertRaises(ReadError) as error:
                self.play._turn_snapshot(self.sid, bid)
            self.assertEqual(error.exception.code, 'route_ended')
            self.assertEqual(len(gateway.inputs), 1)
            self.assertEqual(self.read.journey(self.sid, self.parent)['status'], 'active')

    def test_missing_intent_open_goals_and_foreign_quote_make_no_model_calls(self):
        gateway = self.gateway()
        for planned in (False, True):
            if planned:
                self.play.plan_route_closure(self.sid, self.parent, 'normal')
            with self.assertRaises(ReadError) as error:
                self.propose(self.parent, self.lineage()[-1]['narrativeText'])
            self.assertEqual(error.exception.code, 'ending_not_ready')
        bid, body = self.ready()
        with self.assertRaises(ReadError) as error:
            self.propose(bid, '并不在这条路线正文里的引文')
        self.assertEqual(error.exception.code, 'invalid_ending_evidence')
        self.assertEqual(gateway.inputs, [])
        self.play._planner = SimpleNamespace()
        with self.assertRaises(ReadError) as error:
            self.propose(bid, body)
        self.assertEqual(error.exception.code, 'ending_reviewer_unavailable')
        self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')

    def test_rejected_unknown_and_invalid_reviews_never_commit(self):
        cases = [dict(decision='reject'), dict(decision='unknown'),
                 dict(mutate=lambda v: v['checks'].pop('ending_present')),
                 dict(mutate=lambda v: v['checks']['ending_present'].update(evidence='伪造的正文引文')),
                 dict(mutate=lambda v: v['checks']['ending_present'].update(passed=False))]
        for index, case in enumerate(cases):
            bid, body = self.ready()
            gateway = self.gateway(**case)
            proposal = self.propose(bid, body, str(index))
            self.assertIn(proposal['status'], ('rejected', 'failed'))
            with self.assertRaises(ReadError) as error:
                self.play.commit_ending(self.sid, bid, proposal['id'])
            self.assertEqual(error.exception.code, 'ending_review_required')
            self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')
            self.assertEqual(len(gateway.inputs), 1)

    def test_transport_failure_is_audited_without_retry_or_zero_cost_claim(self):
        bid, body = self.ready()
        gateway = self.gateway(error=LlmError('injected transport failure', 'transport_error'))
        proposal = self.propose(bid, body)
        self.assertEqual(proposal['status'], 'failed')
        self.assertEqual(proposal['audit']['failure']['code'], 'transport_error')
        self.assertEqual(proposal['audit']['metrics']['unreported_calls'], 1)
        self.assertEqual(self.propose(bid, body)['id'], proposal['id'])
        self.assertEqual(len(gateway.inputs), 1)

    def test_duplicate_request_and_restart_reuse_review_and_commit_receipt(self):
        bid, body = self.ready()
        gateway = self.gateway()
        first = self.propose(bid, body)
        self.assertEqual(self.propose(bid, body), first)
        with self.assertRaises(ReadError) as error:
            self.play.propose_ending(self.sid, bid, 'ending-1', '另一份说明', body)
        self.assertEqual(error.exception.code, 'request_conflict')
        restored = PlayService(self.read, self.read.database_path.parent)
        self.addCleanup(restored.drafts.close)
        receipt = restored.commit_ending(self.sid, bid, first['id'])
        self.assertEqual(restored.commit_ending(self.sid, bid, first['id']), receipt)
        self.assertEqual(len(gateway.inputs), 1)
        with self.assertRaises(ReadError):
            restored.end_route(self.sid, bid)

    def test_intent_change_and_change_back_stale_the_review(self):
        bid, body = self.ready()
        self.gateway()
        proposal = self.propose(bid, body)
        self.play.plan_route_closure(self.sid, bid, None)
        self.play.plan_route_closure(self.sid, bid, 'normal')
        self.assertEqual(self.read.ending_proposal(self.sid, bid, proposal['id'])['status'], 'stale')
        with self.assertRaises(ReadError) as error:
            self.play.commit_ending(self.sid, bid, proposal['id'])
        self.assertEqual(error.exception.code, 'ending_proposal_stale')

    def test_text_change_stales_review_and_child_prevents_parent_ending(self):
        bid, body = self.ready()
        self.gateway()
        proposal = self.propose(bid, body)
        self.rewrite(bid, lambda n: n.update(narrativeText=n['narrativeText'] + '你又听见新的动静。'))
        self.assertEqual(self.read.ending_proposal(self.sid, bid, proposal['id'])['status'], 'stale')
        with self.assertRaises(ReadError):
            self.play.commit_ending(self.sid, bid, proposal['id'])
        child = self.append(parent=bid)
        with self.assertRaises(ReadError) as error:
            self.play.commit_ending(self.sid, bid, proposal['id'])
        self.assertEqual(error.exception.code, 'route_has_continuation')
        self.assertEqual(self.read.journey(self.sid, child)['status'], 'active')

    def test_review_can_finish_after_intent_changes_without_ending(self):
        bid, body = self.ready()
        gateway = self.gateway(on_call=lambda: self.play.plan_route_closure(self.sid, bid, None))
        proposal = self.propose(bid, body)
        self.assertEqual(proposal['status'], 'stale')
        self.assertIsNotNone(proposal['audit']['raw_response'])
        self.assertEqual(len(gateway.inputs), 1)
        self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')

    def test_pending_duplicate_does_not_start_second_review(self):
        bid, body = self.ready()
        duplicates = []
        gateway = self.gateway(on_call=lambda: duplicates.append(self.propose(bid, body)))
        proposal = self.propose(bid, body)
        self.assertEqual(duplicates[0]['status'], 'pending')
        self.assertEqual(duplicates[0]['id'], proposal['id'])
        self.assertEqual(len(gateway.inputs), 1)

    def test_early_end_during_review_keeps_early_receipt(self):
        bid, body = self.ready()
        self.gateway(on_call=lambda: self.play.end_route(self.sid, bid))
        proposal = self.propose(bid, body)
        self.assertEqual(proposal['status'], 'stale')
        with self.assertRaises(ReadError):
            self.play.commit_ending(self.sid, bid, proposal['id'])
        self.assertEqual(self.read.route_closure(self.sid, bid)['lifecycle']['ending_type'], 'early')

    def test_deleted_save_is_not_resurrected_by_late_review(self):
        bid, body = self.ready()
        self.gateway(on_call=lambda: self.play.delete_session(self.sid))
        with self.assertRaises(ReadError) as error:
            self.propose(bid, body)
        self.assertEqual(error.exception.status, 404)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM route_lifecycle').fetchone()[0], 0)

    def test_failed_commit_rolls_back_status_receipt_and_proposal(self):
        bid, body = self.ready()
        self.gateway()
        proposal = self.propose(bid, body)
        with patch('open_story_engine.route_endings.save_preferences', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                self.play.commit_ending(self.sid, bid, proposal['id'])
        self.assertEqual(self.read.ending_proposal(self.sid, bid, proposal['id'])['status'], 'approved')
        self.assertIsNone(self.read.route_closure(self.sid, bid)['lifecycle']['receipt'])
        self.assertEqual(self.read.journey(self.sid, bid)['status'], 'active')

    def test_proposals_are_bound_to_branch_and_session(self):
        bid, body = self.ready()
        sibling, _ = self.ready()
        self.gateway()
        proposal = self.propose(bid, body)
        with self.assertRaises(ReadError) as error:
            self.play.commit_ending(self.sid, sibling, proposal['id'])
        self.assertEqual(error.exception.code, 'ending_proposal_not_found')
        with self.assertRaises(ReadError) as error:
            self.read.ending_proposal('foreign', bid, proposal['id'])
        self.assertEqual(error.exception.status, 404)

    def test_list_restores_recent_reviews_and_rechecks_staleness_without_writing(self):
        bid, body = self.ready()
        sibling, _ = self.ready()
        gateway = self.gateway()
        proposals = [self.propose(bid, body, str(i)) for i in range(11)]
        self.play.plan_route_closure(self.sid, bid, None)
        before = self.read.database_path.read_bytes()
        with patch.object(SessionStore, '__init__', side_effect=AssertionError('read initialized writer')):
            recent = self.read.ending_proposals(self.sid, bid)
            self.assertEqual([p['id'] for p in recent], [p['id'] for p in reversed(proposals[-10:])])
            self.assertTrue(all(p['status'] == 'stale' for p in recent))
            self.assertEqual(self.read.ending_proposals(self.sid, sibling), [])
            with self.assertRaises(ReadError):
                self.read.ending_proposals('foreign', bid)
        self.assertEqual(before, self.read.database_path.read_bytes())
        self.assertEqual(len(gateway.inputs), 11)

    def test_http_cannot_supply_approval_and_read_mode_does_not_write(self):
        bid, body = self.ready()
        self.gateway()
        proposal = self.propose(bid, body)
        base = f'/api/v1/sessions/{self.sid}/ending-proposals'
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)) as client:
            forged = dict(branch_id=bid, request_id='forged', outcome_summary='结束', ending_quote=body, approved=True)
            self.assertEqual(client.post(base, json=forged).status_code, 422)
            result = client.post(base + '/' + proposal['id'] + '/commit', json=dict(branch_id=bid))
            self.assertEqual(result.status_code, 200, result.text)
            view = client.get(f'/api/v1/sessions/{self.sid}/route-closure', params=dict(branch_id=bid))
            self.assertEqual(view.status_code, 200, view.text)
            self.assertIs(view.json()['ending_written'], True)
        before = self.read.database_path.read_bytes()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client, \
                patch.object(SessionStore, '__init__', side_effect=AssertionError('read initialized writer')):
            result = client.get(base + '/' + proposal['id'], params=dict(branch_id=bid))
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'committed')
            listed = client.get(base, params=dict(branch_id=bid))
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual([p['id'] for p in listed.json()], [proposal['id']])
            self.assertEqual(listed.json()[0]['status'], 'committed')
            self.assertEqual(client.post(base + '/' + proposal['id'] + '/commit', json=dict(branch_id=bid)).status_code, 404)
        self.assertEqual(before, self.read.database_path.read_bytes())


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformEndingProposal_' + case['package_id'],
                                                        (LongformEndingProposalTests,), {'case': case})))
    return suite
