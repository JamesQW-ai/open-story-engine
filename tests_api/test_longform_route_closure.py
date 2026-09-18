"""Closure preparation checklist on every official long novel; never claims an ending."""
import copy
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from open_story_engine.api import create_app
from open_story_engine.api_read import ReadService
from open_story_engine.reader_consequences import initial_goals
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformRouteClosureTests(unittest.TestCase):
    def setUp(self):
        LongformRepairFallbackTests.setUp(self)
        _, self.package = self.read.load_package(self.case['package_id'], self.case['version'])
        with self.read.store() as store:
            self.contract = store.contract(self.sid)
        self.serial = 0

    def view(self, bid=None):
        return self.read.route_closure(self.sid, bid or self.parent)

    def append(self, parent=None, goals=(), threads=(), outcomes=(), evidence=None, change=None):
        self.serial += 1
        bid = 'closure-fixture-' + str(self.serial)
        parent = parent or self.parent
        body = evidence or '你确认眼前的情况，并决定接下来如何行动。'
        with closing(SessionStore(str(self.read.database_path))) as store:
            old = store.branch(self.sid, parent)
            state = copy.deepcopy(old['branchState'])
            state.setdefault('goalLedger', initial_goals(self.package, self.contract))
            if change:
                change(state)
            changes = dict(goalUpdates=[], threadUpdates=[], outcomes=[])
            for kind, updates, field in [('goal', goals, 'goalLedger'), ('thread', threads, 'threadLedger')]:
                for i, update in enumerate(updates):
                    update = dict(update, evidence=body, reason='本回合明确决定')
                    if kind == 'goal':
                        update.setdefault('dependencies', [])
                    changes[kind + 'Updates'].append(update)
                    record = dict(update, source='player_branch', causeBranchId=bid)
                    if update['id'] == 'new' or update['id'].startswith('new-'):
                        record['id'] = kind + '-' + hashlib.sha256((bid + ':' + str(i)).encode()).hexdigest()[:16]
                        state[field].append(record)
                    else:
                        next(item for item in state[field] if item['id'] == update['id']).update(record)
            for outcome in outcomes:
                record = dict(outcome, evidence=body)
                changes['outcomes'].append(record)
                state.setdefault('characterOutcomeStates', {})[outcome['characterId']] = dict(record, causeBranchId=bid)
            node = dict(id=bid, sourceNodeRef=old['sourceNodeRef'], narrativeText=body, summary=body,
                        branchState=state, consequenceUpdate=changes, nextDirections=[],
                        selectedDirectionId='closure-action', playerDirection=body,
                        selectedDirection=dict(id='closure-action', isFreeText=True, statePatch={}))
            return store.append_branch(self.sid, parent, node)['id']

    def test_opening_lists_obligations_and_never_claims_ending(self):
        result = self.view()
        self.assertEqual(result['version'], 'route-closure/2')
        self.assertEqual(result['status'], 'active')
        self.assertEqual(result['readiness'], 'needs_explanation')
        self.assertFalse(result['ending_written'])
        self.assertIn('不表示自然结局已写成', result['note'])
        self.assertTrue(result['outstanding'])
        kinds = {item['kind'] for item in result['outstanding']}
        self.assertIn('goal', kinds)
        self.assertIn('thread', kinds)
        self.assertTrue(all(item['required_disclosure'] for item in result['outstanding']))

    def test_completed_and_resolved_with_evidence_clear_checklist_without_ending(self):
        with self.read.store() as store:
            goals = initial_goals(self.package, self.contract)
            threads = store.branch(self.sid, self.parent)['branchState']['threadLedger']
        bid = self.append(
            goals=[dict(id=g['id'], title=g['title'], status='completed') for g in goals],
            threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads])
        result = self.view(bid)
        self.assertEqual(result['readiness'], 'checklist_clear')
        self.assertEqual(result['outstanding'], [])
        self.assertFalse(result['ending_written'])
        self.assertTrue(result['cleared'])
        self.assertEqual(result['cleared_count'], len(result['cleared']))

    def test_closing_phase_is_derived_from_the_current_route_ledger(self):
        from open_story_engine.reader_consequences import _closing_phase

        with self.read.store() as store:
            contract = store.contract(self.sid)
            opening = store.branch(self.sid, self.parent)
        intent = {'intended_type': 'normal', 'branch_id': self.parent, 'revision': 1}
        self.assertFalse(_closing_phase(dict(package=self.package, contract=contract,
                                             parent=opening, lineage=[opening],
                                             closingIntent=intent)))

        bid = self.append(
            goals=[dict(id=g['id'], title=g['title'], status='completed')
                   for g in initial_goals(self.package, contract)],
            threads=[dict(id=t['id'], title=t['title'], status='resolved')
                     for t in opening['branchState']['threadLedger']])
        with self.read.store() as store:
            nodes = store.lineage(self.sid, bid)
        self.assertTrue(_closing_phase(dict(package=self.package, contract=contract,
                                            parent=nodes[-1], lineage=nodes,
                                            closingIntent={**intent, 'branch_id': bid})))

    def test_dependency_block_lists_goal_and_dependency_items(self):
        cid = next(c['id'] for c in self.package['characters'] if c['id'] != self.cid)
        goal = initial_goals(self.package, self.contract)[0]
        bid = self.append(
            goals=[dict(id=goal['id'], title=goal['title'], status='active', dependencies=[cid])],
            outcomes=[dict(characterId=cid, status='dead', permanence='permanent')],
            evidence='你确认他已倒下，再无法继续同行。')
        result = self.view(bid)
        self.assertEqual(result['readiness'], 'blocked_dependency')
        kinds = [item['kind'] for item in result['outstanding']]
        self.assertIn('goal', kinds)
        self.assertIn('dependency', kinds)
        dependency = next(i for i in result['outstanding'] if i['kind'] == 'dependency')
        self.assertEqual(dependency['blockers'], [cid])
        self.assertIn('dead', dependency['reason'])
        self.assertFalse(result['ending_written'])

    def test_unknown_ledger_coverage_blocks_readiness(self):
        bid = self.append()

        def strip(state):
            state.pop('goalLedger', None)
            state.pop('threadLedger', None)

        # A later branch without ledgers is unknown, not cleared.
        child = self.append(parent=bid, change=strip)
        result = self.view(child)
        self.assertEqual(result['coverage']['goals'], 'unknown')
        self.assertEqual(result['coverage']['threads'], 'unknown')
        self.assertEqual(result['readiness'], 'blocked_unknown')
        self.assertTrue(any('未确认' in item['reason'] for item in result['outstanding']))
        self.assertFalse(result['ending_written'])

    def test_ended_route_keeps_outstanding_and_ending_written_false(self):
        result = self.view()
        self.assertTrue(result['outstanding'])
        self.play.end_route(self.sid, self.parent)
        ended = self.view()
        self.assertEqual(ended['status'], 'abandoned')
        self.assertEqual(ended['readiness'], 'ended')
        self.assertEqual(ended['outstanding_count'], result['outstanding_count'])
        self.assertFalse(ended['ending_written'])

    def test_siblings_do_not_share_closure_checklist(self):
        with self.read.store() as store:
            goals = initial_goals(self.package, self.contract)
            threads = store.branch(self.sid, self.parent)['branchState']['threadLedger']
        sibling = self.append(goals=[
            dict(id=g['id'], title=g['title'], status='completed') for g in goals
        ], threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads])
        self.assertEqual(self.view(sibling)['readiness'], 'checklist_clear')
        self.assertEqual(self.view()['readiness'], 'needs_explanation')

    def test_http_is_read_only_and_rejects_cross_session_branch(self):
        bid = self.append()
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client, \
                patch.object(SessionStore, '__init__', side_effect=AssertionError('read must not initialize a writer')):
            response = client.get(f'/api/v1/sessions/{self.sid}/route-closure', params={'branch_id': bid})
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload['branch_id'], bid)
            self.assertIs(payload['ending_written'], False)
            self.assertIn('不表示自然结局已写成', payload['note'])
            self.assertEqual(client.get(f'/api/v1/sessions/{self.sid}/route-closure').status_code, 422)
            self.assertEqual(client.get(f'/api/v1/sessions/{self.sid}/route-closure',
                                        params={'branch_id': 'unrelated'}).status_code, 404)
            self.assertEqual(client.get('/api/v1/sessions/unrelated/route-closure',
                                        params={'branch_id': bid}).status_code, 404)
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_invalid_history_returns_controlled_error_without_migration(self):
        bid = self.append()
        with sqlite3.connect(self.read.database_path) as db:
            row = db.execute('SELECT node_json FROM branch_nodes WHERE id=?', (bid,)).fetchone()
            node = json.loads(row[0])
            node['branchState']['goalLedger'] = 'invalid-ledger'
            db.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node), bid))
        before = self.read.database_path.read_bytes()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client:
            response = client.get(f'/api/v1/sessions/{self.sid}/route-closure', params={'branch_id': bid})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['error']['code'], 'route_history_unavailable')
        self.assertEqual(before, self.read.database_path.read_bytes())

    def test_absent_database_is_not_created(self):
        missing = Path(self.temp) / 'missing-closure.sqlite'
        with TestClient(create_app(ROOT / 'content/packages', missing, play=False)) as client:
            response = client.get('/api/v1/sessions/missing/route-closure',
                                  params={'branch_id': self.parent})
            self.assertEqual(response.status_code, 404)
        self.assertFalse(missing.exists())

    def lineage(self, bid=None):
        with closing(SessionStore(str(self.read.database_path))) as store:
            return store.lineage(self.sid, bid or self.parent)

    def test_planning_context_carries_outstanding_closure_without_ending_claim(self):
        from open_story_engine import reader_consequences as rc
        from open_story_engine.route_closure import closure_projection
        nodes = self.lineage()
        projection = closure_projection(self.package, self.contract, nodes)
        self.assertEqual(projection['readiness'], 'needs_explanation')
        self.assertTrue(projection['outstanding'])
        self.assertIn('不表示自然结局已写成', projection['note'])
        self.assertTrue(all(i['required_disclosure'] for i in projection['outstanding']))
        self.assertTrue(all(set(i) == {'kind', 'target_id', 'title', 'reason',
                                       'required_disclosure', 'blockers'} for i in projection['outstanding']))
        context = dict(package=self.package, contract=self.contract,
                       parent=nodes[-1], lineage=nodes)
        planned = rc.planning_context(context)
        self.assertEqual(planned['closure'], projection)

    def test_planning_context_closure_is_unknown_when_outline_breaks_not_false_clear(self):
        from open_story_engine import reader_consequences as rc
        nodes = self.lineage()
        broken = copy.deepcopy(nodes)
        broken[-1]['branchState']['goalLedger'] = 'invalid'
        context = dict(package=self.package, contract=self.contract,
                       parent=broken[-1], lineage=broken)
        planned = rc.planning_context(context)
        self.assertEqual(planned['closure']['readiness'], 'unknown')
        self.assertEqual(planned['closure']['outstanding'], [])
        self.assertIn('不得据此推断', planned['closure']['note'])

    def test_planning_context_closure_clear_only_after_evidence(self):
        from open_story_engine import reader_consequences as rc
        with self.read.store() as store:
            goals = initial_goals(self.package, self.contract)
            threads = store.branch(self.sid, self.parent)['branchState']['threadLedger']
        bid = self.append(
            goals=[dict(id=g['id'], title=g['title'], status='completed') for g in goals],
            threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads])
        nodes = self.lineage(bid)
        context = dict(package=self.package, contract=self.contract,
                       parent=nodes[-1], lineage=nodes)
        planned = rc.planning_context(context)
        self.assertEqual(planned['closure']['readiness'], 'checklist_clear')
        self.assertEqual(planned['closure']['outstanding'], [])
        self.assertIn('不表示自然结局已写成', planned['closure']['note'])

    def test_journey_route_health_exposes_stall_and_closure_without_ending(self):
        journal = self.read.journey(self.sid, self.parent)
        health = journal['route_health']
        self.assertIs(health['ending_written'], False)
        self.assertEqual(health['closure_readiness'], 'needs_explanation')
        self.assertGreater(health['closure_outstanding_count'], 0)
        self.assertIn('不表示自然结局已写成', health['note'])
        self.assertEqual(journal['progress'], 0)  # the official graph exposes chapter_001..chapter_343
        self.assertEqual(journal['progress_label'], '原著路线进度')
        # Stall window after repeated wait turns.
        bid = self.parent
        for i in range(3):
            bid = self.append(parent=bid, evidence='你仍留在原处，没有改变当前安排。')
        stalled = self.read.journey(self.sid, bid)['route_health']
        self.assertIn('unchanged_tracked_state', stalled['signals'])
        self.assertTrue(stalled['review_recommended'])
        self.assertEqual(stalled['window'], 3)
        self.assertEqual(stalled['unchanged_state_turns'], 3)
        self.assertIs(stalled['ending_written'], False)

    def test_journey_route_health_unknown_not_clear_when_ledgers_broken(self):
        bid = self.append()

        def strip(state):
            state['goalLedger'] = 'broken'

        broken = self.append(parent=bid, change=strip)
        health = self.read.journey(self.sid, broken)['route_health']
        self.assertEqual(health['closure_readiness'], 'unknown')
        self.assertIs(health['ending_written'], False)

    def test_empty_ledgers_cannot_erase_opening_obligations(self):
        bid = self.append(change=lambda state: state.update(goalLedger=[], threadLedger=[]))
        result = self.view(bid)
        self.assertEqual(result['readiness'], 'blocked_unknown')
        self.assertEqual(result['coverage']['goals'], 'unknown')
        self.assertEqual(result['coverage']['threads'], 'unknown')
        self.assertEqual({i['target_id'] for i in result['outstanding']},
                         {i['target_id'] for i in self.view()['outstanding']})
        health = self.read.journey(self.sid, bid)['route_health']
        self.assertEqual(health['closure_readiness'], 'blocked_unknown')

    def test_empty_opening_ledgers_are_not_a_valid_clear_checklist(self):
        from open_story_engine.route_closure import preparation
        nodes = copy.deepcopy(self.lineage())
        nodes[0]['branchState'].update(goalLedger=[], threadLedger=[])
        result = preparation(self.package, self.contract, nodes, 'active')
        self.assertEqual(result['readiness'], 'blocked_unknown')
        self.assertTrue(result['outstanding'])

    def test_removed_new_goal_and_thread_remain_unknown_in_planning(self):
        from open_story_engine.route_closure import closure_projection
        bid = self.append(goals=[dict(id='new', title='确认安全出口', status='active')],
                          threads=[dict(id='new-question', title='出口通向何处', status='open')])
        # Remove at the origin as well: the committed updates still name them.
        nodes = copy.deepcopy(self.lineage(bid))
        for field in ('goalLedger', 'threadLedger'):
            nodes[-1]['branchState'][field].pop()
        projection = closure_projection(self.package, self.contract, nodes)
        self.assertEqual(projection['readiness'], 'blocked_unknown')
        missing = [i for i in projection['outstanding'] if '缺失' in i['reason']]
        self.assertEqual({i['title'] for i in missing}, {'确认安全出口', '出口通向何处'})

    def test_evidenced_abandonment_is_disposition_not_goal_success(self):
        with self.read.store() as store:
            threads = store.branch(self.sid, self.parent)['branchState']['threadLedger']
        bid = self.append(goals=[dict(id=g['id'], title=g['title'], status='abandoned')
                                 for g in initial_goals(self.package, self.contract)],
                          threads=[dict(id=t['id'], title=t['title'], status='abandoned') for t in threads])
        result = self.view(bid)
        self.assertEqual(result['readiness'], 'checklist_clear')
        self.assertFalse(result['ending_written'])
        self.assertTrue(all('不表示目标达成' in i['reason'] for i in result['cleared'] if i['kind'] == 'goal'))
        self.assertTrue(all('不表示问题已查明' in i['reason'] for i in result['cleared'] if i['kind'] == 'thread'))
        outline = self.read.route_outline(self.sid, bid)['outline']
        self.assertTrue(all(g['completion']['met'] is False for g in outline['goals']))

    def test_transformation_requires_successor_and_tracks_it_separately(self):
        from open_story_engine.route_closure import preparation
        old = initial_goals(self.package, self.contract)[0]
        bid = self.append(goals=[dict(id=old['id'], title=old['title'], status='transformed', successor='先确认安全出口')])
        missing = self.view(bid)
        self.assertEqual(missing['readiness'], 'blocked_unknown')
        successor_id = 'goal-' + hashlib.sha256((bid + ':0').encode()).hexdigest()[:16]
        self.assertIn(successor_id, [i['target_id'] for i in missing['outstanding']])
        nodes = copy.deepcopy(self.lineage(bid))
        previous = nodes[-1]['branchState']['goalLedger'][0]
        nodes[-1]['branchState']['goalLedger'].append(dict(previous, id=successor_id, title='先确认安全出口',
                                                         status='active', previousGoalId=old['id']))
        result = preparation(self.package, self.contract, nodes, 'active')
        self.assertEqual(result['readiness'], 'needs_explanation')
        self.assertNotIn(old['id'], [i['target_id'] for i in result['outstanding']])
        self.assertIn(successor_id, [i['target_id'] for i in result['outstanding']])
        self.assertIn(old['id'], [i['target_id'] for i in result['cleared']])

    def test_missing_character_is_not_described_as_permanently_offline(self):
        cid = next(c['id'] for c in self.package['characters'] if c['id'] != self.cid)
        old = initial_goals(self.package, self.contract)[0]
        bid = self.append(goals=[dict(id=old['id'], title=old['title'], status='active', dependencies=[cid])],
                          outcomes=[dict(characterId=cid, status='missing', permanence='temporary')])
        result = self.view(bid)
        self.assertEqual(result['readiness'], 'blocked_dependency')
        self.assertNotIn('永久', json.dumps(result, ensure_ascii=False))
        recovered = self.append(parent=bid, outcomes=[dict(characterId=cid, status='alive', permanence='temporary')])
        self.assertEqual(self.view(recovered)['readiness'], 'needs_explanation')

    def test_historical_ledger_gap_is_unknown_even_with_current_completed_records(self):
        from open_story_engine.route_closure import preparation
        middle = self.append()
        with self.read.store() as store:
            threads = store.branch(self.sid, middle)['branchState']['threadLedger']
        last = self.append(parent=middle,
                           goals=[dict(id=g['id'], title=g['title'], status='completed')
                                  for g in initial_goals(self.package, self.contract)],
                           threads=[dict(id=t['id'], title=t['title'], status='resolved') for t in threads])
        self.assertEqual(self.view(last)['readiness'], 'checklist_clear')
        nodes = copy.deepcopy(self.lineage(last))
        nodes[1]['branchState'].pop('goalLedger')
        result = preparation(self.package, self.contract, nodes, 'active')
        self.assertEqual(result['readiness'], 'blocked_unknown')
        self.assertEqual(result['coverage']['goals'], 'unknown')
        self.assertTrue(any(i['target_id'] == 'goalLedger:history' for i in result['outstanding']))

    def test_broken_historical_ledger_returns_unknown_projection(self):
        from open_story_engine.route_closure import closure_projection
        middle = self.append()
        last = self.append(parent=middle)
        nodes = copy.deepcopy(self.lineage(last))
        nodes[1]['branchState']['threadLedger'] = 'broken'
        projection = closure_projection(self.package, self.contract, nodes)
        self.assertEqual(projection['readiness'], 'unknown')


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(
            type('LongformRouteClosure_' + case['package_id'], (LongformRouteClosureTests,), {'case': case})))
    return suite
