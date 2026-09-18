"""Branch-bound outline contracts on the entire official long-novel corpus."""
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
from open_story_engine.api_read import ReadService, ReadError
from open_story_engine.reader_consequences import initial_goals
from open_story_engine.route_outline import build_outline
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformRouteOutlineTests(unittest.TestCase):
    def setUp(self):
        LongformRepairFallbackTests.setUp(self)
        _, self.package = self.read.load_package(self.case['package_id'], self.case['version'])
        with self.read.store() as store:
            self.contract = store.contract(self.sid)
        self.serial = 0

    def view(self, bid=None):
        return self.read.route_outline(self.sid, bid or self.parent)

    def append(self, parent=None, goals=(), threads=(), outcomes=(), evidence=None):
        """Inject explicit ledger scenarios, without pretending model review passed."""
        self.serial += 1
        bid = 'outline-fixture-' + str(self.serial)
        parent = parent or self.parent
        body = evidence or '你确认眼前的情况，并决定接下来如何行动。'
        with closing(SessionStore(str(self.read.database_path))) as store:
            old = store.branch(self.sid, parent)
            state = copy.deepcopy(old['branchState'])
            state.setdefault('goalLedger', initial_goals(self.package, self.contract))
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
                state['characterOutcomeStates'][outcome['characterId']] = dict(record, causeBranchId=bid)
            node = dict(id=bid, sourceNodeRef=old['sourceNodeRef'], narrativeText=body, summary=body,
                        branchState=state, consequenceUpdate=changes, nextDirections=[],
                        selectedDirectionId='outline-action', playerDirection=body,
                        selectedDirection=dict(id='outline-action', isFreeText=True, statePatch={}))
            return store.append_branch(self.sid, parent, node)['id']

    def rewrite(self, bid, fn):
        with sqlite3.connect(self.read.database_path) as db:
            node = json.loads(db.execute('SELECT node_json FROM branch_nodes WHERE id=?', (bid,)).fetchone()[0])
            fn(node)
            db.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(node, ensure_ascii=False), bid))

    def test_all_roles_start_with_saved_source_bound_obligations(self):
        for cid in self.package['story']['entryModel']['sourceCharacterIds']:
            character = next(c for c in self.package['characters'] if c['id'] == cid)
            start = self.play.create_session(self.case['package_id'], self.case['version'], character['defaultEntryPointId'], cid, identity_opening=True)
            result = self.read.route_outline(start['session']['id'], start['branch']['id'])
            self.assertEqual(result['storage'], 'saved')
            self.assertTrue(result['outline']['goals'])
            self.assertTrue(all(g['status'] == 'active' and g['evidence']['kind'] == 'opening' for g in result['outline']['goals']))
            self.assertIsNone(result['outline']['structure']['volume'])
            progress = result['outline']['structure']['source_progress']
            self.assertIsInstance(progress, str)
            expected_arc = next(arc['id'] for arc in self.package['story']['arcModel']['arcs']
                                if progress in arc.get('availableWhen', {}).get('sourceProgress', {}).get('oneOf', []))
            self.assertEqual(result['outline']['structure']['arc'], expected_arc)
            self.assertEqual(result['outline']['structure']['beat'], 'beat_' + progress)

    def test_structure_stays_unknown_when_source_progress_is_missing(self):
        self.rewrite(self.parent, lambda node: node['branchState'].pop('sourceProgress', None))
        structure = self.view()['outline']['structure']
        self.assertIsNone(structure['volume'])
        self.assertIsNone(structure['arc'])
        self.assertIsNone(structure['beat'])
        self.assertIsNone(structure['source_progress'])

    def test_real_commit_saves_outline_atomically_and_restart_restores_it(self):
        turn = self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='outline-turn')
        bid = turn['branch']['id']
        result = self.view(bid)
        self.assertEqual(result['storage'], 'saved')
        self.assertEqual(turn['branch']['routeOutline'], result['outline'])
        restored = ReadService(ROOT / 'content/packages', self.read.database_path)
        self.assertEqual(restored.route_outline(self.sid, bid), result)
        duplicate = self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='outline-turn')
        self.assertTrue(duplicate['deduplicated'])
        self.assertEqual(duplicate['branch']['routeOutline'], result['outline'])

    def test_goal_deviation_drops_old_task_but_preserves_sibling_history(self):
        old = initial_goals(self.package, self.contract)[0]
        left = self.append(goals=[dict(id=old['id'], title=old['title'], status='abandoned'),
                                  dict(id='new', title='保护自身安全', status='active')],
                           evidence='你决定放下原先的追查，接下来只保护自身安全。')
        right = self.append(evidence='你仍然留在原地观察。')
        result = self.view(left)['outline']
        self.assertEqual(next(g for g in result['goals'] if g['id'] == old['id'])['status'], 'abandoned')
        self.assertNotIn('goal:' + old['id'], [s['id'] for s in result['steps']])
        self.assertIn('保护自身安全', [s['title'] for s in result['steps']])
        self.assertIn('goal:' + old['id'], [s['id'] for s in self.view(right)['outline']['steps']])
        self.assertNotEqual(result['binding_digest'], self.view(right)['outline']['binding_digest'])

    def test_transformed_goal_uses_successor_instead_of_reopening_old_goal(self):
        old = initial_goals(self.package, self.contract)[0]
        bid = self.append(goals=[dict(id=old['id'], title=old['title'], status='transformed', successor='先保障自身安全')])
        def add_successor(node):
            previous = node['branchState']['goalLedger'][0]
            new_id = 'goal-' + hashlib.sha256((bid + ':0').encode()).hexdigest()[:16]
            node['branchState']['goalLedger'].append(dict(previous, id=new_id, title='先保障自身安全',
                                                         status='active', previousGoalId=old['id']))
        self.rewrite(bid, add_successor)
        outline = self.view(bid)['outline']
        self.assertEqual(outline['goals'][-1]['status'], 'active')
        self.assertIsNotNone(outline['goals'][-1]['evidence'])
        self.assertNotIn('goal:' + old['id'], [s['id'] for s in outline['steps']])
        self.assertIn('先保障自身安全', [s['title'] for s in outline['steps']])

    def test_outline_failure_rolls_back_turn_and_retry_reuses_ready_body(self):
        with self.read.store() as store:
            before = len(store.branches(self.sid))
        with patch('open_story_engine.route_outline.build_outline', side_effect=ValueError('injected outline failure')):
            with self.assertRaises(ReadError):
                self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='outline-rollback')
        with self.read.store() as store:
            self.assertEqual(len(store.branches(self.sid)), before)
            self.assertIsNone(store.find_branch_request(self.sid, 'outline-rollback'))
        with patch.object(self.play.drafts, 'generate', side_effect=AssertionError('ready body must not regenerate')):
            result = self.play.continue_turn(self.sid, self.parent, text='留在原地观察周围', request_id='outline-rollback')
        self.assertEqual(self.view(result['branch']['id'])['storage'], 'saved')

    def test_opening_outline_failure_does_not_leave_partial_session(self):
        with sqlite3.connect(self.read.database_path) as db:
            before = list(db.iterdump())
        character = next(c for c in self.package['characters'] if c['id'] == self.cid)
        with patch('open_story_engine.route_outline.build_outline', side_effect=ValueError('injected outline failure')):
            with self.assertRaises(ReadError):
                self.play.create_session(self.case['package_id'], self.case['version'], character['defaultEntryPointId'],
                                         self.cid, identity_opening=True, request_id='outline-opening-rollback')
        with sqlite3.connect(self.read.database_path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_matching_lineage_evidence_is_required_for_completion(self):
        old = initial_goals(self.package, self.contract)[0]
        left = self.append(goals=[dict(id=old['id'], title=old['title'], status='completed')])
        result = self.view(left)
        record = next(g for g in result['outline']['goals'] if g['id'] == old['id'])
        self.assertTrue(record['completion']['met'])
        self.assertEqual(result['status'], 'active')
        right = self.append()
        with self.read.store() as store:
            borrowed = store.branch(self.sid, left)['branchState']['goalLedger'][0]
        self.rewrite(right, lambda node: node['branchState']['goalLedger'].__setitem__(0, borrowed))
        record = self.view(right)['outline']['goals'][0]
        self.assertEqual(record['status'], 'unknown')
        self.assertIsNone(record['completion']['met'])
        self.rewrite(left, lambda node: node.update(narrativeText='这段正文不含原先的完成依据。'))
        self.assertEqual(self.view(left)['outline']['goals'][0]['status'], 'unknown')

    def test_unavailable_dependency_requires_replanning_and_recovery_removes_conflict(self):
        cid = next(c['id'] for c in self.package['characters'] if c['id'] != self.cid)
        old = initial_goals(self.package, self.contract)[0]
        first = self.append(goals=[dict(id=old['id'], title=old['title'], status='active', dependencies=[cid])],
                            outcomes=[dict(characterId=cid, status='missing', permanence='temporary')])
        outline = self.view(first)['outline']
        self.assertEqual(len(outline['conflicts']), 1)
        step = next(s for s in outline['steps'] if s['target_id'] == old['id'])
        self.assertEqual(step['kind'], 'review_dependency')
        self.assertTrue(step['blockers'])
        recovered = self.append(first, outcomes=[dict(characterId=cid, status='alive', permanence='temporary')],
                                evidence='你在眼前重新见到了对方，确认对方仍然存活。')
        self.assertFalse(self.view(recovered)['outline']['conflicts'])
        for status in ('dead', 'departed'):
            terminal = self.append(first, outcomes=[dict(characterId=cid, status=status, permanence='permanent')])
            self.assertEqual(self.view(terminal)['outline']['conflicts'][0]['status'], status)

    def test_unsupported_dependency_or_outcome_does_not_invent_conflict(self):
        old = initial_goals(self.package, self.contract)[0]
        bid = self.append(goals=[dict(id=old['id'], title=old['title'], status='active')])
        cid = next(c['id'] for c in self.package['characters'] if c['id'] != self.cid)
        def tamper(node):
            node['branchState']['goalLedger'][0]['dependencies'] = [cid]
            node['branchState']['characterOutcomeStates'][cid] = dict(status='dead', permanence='permanent')
        self.rewrite(bid, tamper)
        outline = self.view(bid)['outline']
        self.assertFalse(outline['conflicts'])
        self.assertEqual(outline['goals'][0]['status'], 'unknown')

    def test_thread_closure_has_evidence_and_ended_route_stays_nonactionable(self):
        with self.read.store() as store:
            thread = store.branch(self.sid, self.parent)['branchState']['threadLedger'][0]
        bid = self.append(threads=[dict(id=thread['id'], title=thread['title'], status='abandoned')])
        before = self.view(bid)
        self.assertTrue(before['outline']['threads'][0]['completion']['met'])
        self.assertNotIn('thread:' + thread['id'], [s['id'] for s in before['outline']['steps']])
        self.play.end_route(self.sid, bid)
        after = self.view(bid)
        self.assertFalse(after['actionable'])
        self.assertEqual(after['outline'], before['outline'])

    def test_stale_or_injected_saved_outline_cannot_change_truth(self):
        expected = self.view()['outline']
        self.rewrite(self.parent, lambda n: n['routeOutline'].update(steps=[], goals=[], binding_digest='forged'))
        before = self.read.database_path.read_bytes()
        actual = self.view()
        self.assertEqual(actual['storage'], 'reconstructed')
        self.assertEqual(actual['outline'], expected)
        self.assertEqual(before, self.read.database_path.read_bytes())

    def test_legacy_missing_ledgers_stay_unknown(self):
        bid = self.append()
        def legacy(n):
            n.pop('routeOutline', None)
            n['branchState'].pop('goalLedger')
            n['branchState'].pop('threadLedger')
        self.rewrite(bid, legacy)
        result = self.view(bid)
        self.assertEqual(result['storage'], 'reconstructed')
        self.assertTrue(all(s['kind'] == 'verify_status' and s['completion']['met'] is None for s in result['outline']['steps']))

    def test_http_read_only_and_invalid_history(self):
        before = self.read.database_path.read_bytes()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client, \
                patch.object(SessionStore, '__init__', side_effect=AssertionError('read cannot initialize writer')):
            url = f'/api/v1/sessions/{self.sid}/route-outline'
            response = client.get(url, params={'branch_id': self.parent})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['storage'], 'saved')
            self.assertEqual(client.get(url).status_code, 422)
            self.assertEqual(client.get(url, params={'branch_id': 'foreign'}).status_code, 404)
            self.assertEqual(client.get('/api/v1/sessions/foreign/route-outline', params={'branch_id': self.parent}).status_code, 404)
        self.assertEqual(before, self.read.database_path.read_bytes())
        self.rewrite(self.parent, lambda n: n['branchState'].update(goalLedger='invalid'))
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client:
            response = client.get(url, params={'branch_id': self.parent})
            self.assertEqual(response.status_code, 409, response.text)

    def test_missing_database_not_created(self):
        path = Path(self.temp) / 'absent.sqlite'
        with TestClient(create_app(ROOT / 'content/packages', path, play=False)) as client:
            self.assertEqual(client.get('/api/v1/sessions/absent/route-outline', params={'branch_id': self.parent}).status_code, 404)
        self.assertFalse(path.exists())


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformRouteOutline_' + case['package_id'], (LongformRouteOutlineTests,), {'case': case})))
    return suite
