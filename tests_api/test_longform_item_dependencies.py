"""Item-dependent obligations retain status and require evidenced replanning."""
import copy
import hashlib
from contextlib import closing
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from open_story_engine.api import create_app
from open_story_engine import reader_consequences as rc, item_lifecycle as items
from open_story_engine.reader_choices import choice_context
from open_story_engine.route_outline import build_outline
from open_story_engine.route_closure import preparation
from open_story_engine.route_monitor import monitor
from open_story_engine.storage import SessionStore
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_item_destruction import LongformItemDestructionTests


class LongformItemDependencyTests(unittest.TestCase):
    setUp = LongformItemDestructionTests.setUp
    result = LongformItemDestructionTests.result

    def goal(self, **changes):
        return dict(dict(id='new', title='利用现有物品核对眼前线索', status='active', dependencies=[],
                         itemDependencies=[self.iid], reason='明确采用当前物品作为核查工具'), **changes)

    def thread(self, **changes):
        return dict(dict(id='new-1', title='现有物品能否证明眼前线索', status='open', stepIds=['S1'],
                         itemDependencies=[self.iid], reason='登记尚待查证的问题'), **changes)

    def advance(self, action, body, goals=(), threads=(), destroy=False):
        self.context['playerDirection'] = action
        result = self.result()
        plan = result['consequenceUpdate']
        plan['goalUpdates'] = [dict(g, evidence=body) for g in goals]
        plan['threadUpdates'] = [dict(t, evidence=body) for t in threads]
        result['narrativeText'] = body
        result['reviewedNarrativeSha256'] = hashlib.sha256(body.encode()).hexdigest()
        result['observedEvents'][0]['summary'] = body
        if destroy:
            plan['stateChanges'][0]['evidence'] = body
        else:
            plan['stateChanges'] = []
            plan['steps'][0]['usedItemIds'] = []
            result['authorityReview']['stateChecks'] = []
            result['observedEvents'][0].update(changes=[], usedItemIds=[])
            result['eventChecks'][0]['changeIds'] = []
        bid = 'item-deps-' + str(len(self.context['lineage']))
        state = rc.commit_consequences(self.context, self.state, result, bid)
        node = dict(self.context['parent'], **result)
        node.update(id=bid, branchState=state, selectedDirectionId='item-deps-action',
                    selectedDirection=dict(id='item-deps-action', isFreeText=True, statePatch={}),
                    playerDirection=action, nextDirections=[])
        with closing(SessionStore(str(self.read.database_path))) as store:
            store.append_branch(self.sid, self.context['parent']['id'], node)
        with self.read.store() as store:
            lineage = store.lineage(self.sid, bid)
        self.context.update(parent=lineage[-1], lineage=lineage)
        self.state = lineage[-1]['branchState']
        return lineage[-1]

    def establish(self):
        node = self.advance('记下用现有物品核查线索的计划',
            '你决定用' + self.item['name'] + '核查眼前线索，记下目标和仍待查证的问题。',
            [self.goal()], [self.thread()])
        self.gid = self.state['goalLedger'][-1]['id']
        self.tid = self.state['threadLedger'][-1]['id']
        return node

    def destroy(self):
        return self.advance('彻底毁掉' + self.item['name'],
            '你将' + self.item['name'] + '彻底毁去，原物无法修复。原先的目标与问题仍未交代。', destroy=True)

    def outline(self, nodes=None):
        return build_outline(self.package, self.context['contract'], nodes or self.context['lineage'])

    def test_destruction_blocks_both_obligations_without_closing_or_forcing_abandonment(self):
        first = self.establish()
        node = self.destroy()
        self.assertEqual(self.state['goalLedger'][-1]['status'], 'active')
        self.assertEqual(self.state['threadLedger'][-1]['status'], 'open')
        outline = self.outline()
        conflicts = outline['conflicts']
        self.assertEqual({c['target_kind'] for c in conflicts}, {'goal', 'thread'})
        self.assertTrue(all(c['status'] == 'destroyed' and c['evidence']['branch_id'] == node['id'] for c in conflicts))
        steps = [s for s in outline['steps'] if s['target_id'] in (self.gid, self.tid)]
        self.assertTrue(all(s['kind'] == 'review_dependency' and s['blockers'] for s in steps))
        closure = preparation(self.package, self.context['contract'], self.context['lineage'], 'active')
        self.assertEqual(closure['readiness'], 'blocked_dependency')
        self.assertEqual(len([i for i in closure['outstanding'] if i['kind'] == 'dependency']), 2)
        self.assertFalse(closure['ending_written'])
        self.assertEqual(rc.planning_context(self.context)['closure']['readiness'], 'blocked_dependency')
        with self.read.store() as store:
            old_nodes = store.lineage(self.sid, first['id'])
        self.assertFalse(self.outline(old_nodes)['conflicts'])
        public = choice_context(self.package, self.context['contract'], self.context['lineage'][:-1], node)
        self.assertEqual(public['goals'][-1]['itemDependencies'], [self.iid])
        self.assertEqual(public['threads'][-1]['itemDependencies'], [self.iid])
        fallback_state = dict(self.state, freeTextProgress=1)
        self.assertFalse(any(d['id'].startswith('goal-direction-' + self.gid + '-')
                             for d in rc.goal_directions(fallback_state)))

    def test_invalid_and_same_turn_destroyed_dependencies_are_rejected(self):
        for collection, make in (('goalUpdates', self.goal), ('threadUpdates', self.thread)):
            for deps in (None, self.iid, [self.cid], ['item_unregistered'], [self.iid, self.iid], [self.iid]):
                plan = self.result()['consequenceUpdate']
                plan[collection] = [make(itemDependencies=deps)]
                with self.subTest(collection=collection, deps=deps), self.assertRaisesRegex(ValueError, '道具'):
                    rc.validate_plan(plan, {'A1': self.context['playerDirection']}, self.context)
        self.establish()
        self.destroy()
        plan = self.result()['consequenceUpdate']
        plan['stateChanges'] = []
        plan['steps'][0]['usedItemIds'] = []
        for collection, make in (('goalUpdates', self.goal), ('threadUpdates', self.thread)):
            candidate = dict(plan, **{collection: [make()]})
            with self.assertRaisesRegex(ValueError, '永久损毁'):
                rc.validate_plan(candidate, {'A1': self.context['playerDirection']}, self.context)

    def test_explicit_evidenced_dependency_change_keeps_titles_and_clears_blockers(self):
        self.establish()
        self.destroy()
        result = self.result()
        plan = result['consequenceUpdate']
        plan['stateChanges'] = []
        plan['steps'][0]['usedItemIds'] = []
        plan['goalUpdates'] = [self.goal(id=self.gid, itemDependencies=[])]
        plan['threadUpdates'] = [self.thread(id=self.tid, itemDependencies=[])]
        checked = rc.validate_plan(plan, {'A1': self.context['playerDirection']}, self.context)
        self.assertEqual(len(checked['goalUpdates']), 1)  # Dependency-only update is not discarded.
        result['authorityReview']['stateChecks'] = []
        result['observedEvents'][0].update(changes=[], usedItemIds=[])
        result['eventChecks'][0]['changeIds'] = []
        with self.assertRaisesRegex(ValueError, '结果证据'):
            rc.commit_consequences(self.context, self.state, result, 'unreviewed-deps')
        node = self.advance('改用现场观察追查线索', '你决定改用现场观察追查同一线索，不再需要已经损毁的原物。',
            [self.goal(id=self.gid, itemDependencies=[])], [self.thread(id=self.tid, itemDependencies=[])])
        self.assertEqual(node['branchState']['goalLedger'][-1]['status'], 'active')
        self.assertEqual(node['branchState']['threadLedger'][-1]['status'], 'open')
        self.assertFalse(self.outline()['conflicts'])
        self.assertTrue(items.destroyed(self.state, self.iid))
        signals = monitor(self.context['lineage'], self.package, self.context['contract'], 'active')
        self.assertEqual(set(signals['recent_turns'][-1]['changed']), {'goals', 'threads'})
        self.assertEqual(signals['unchanged_state_turns'], 0)

    def test_omission_inherits_and_closing_obligations_preserves_dependency_history(self):
        self.establish()
        self.destroy()
        goal, thread = self.goal(id=self.gid, status='abandoned'), self.thread(id=self.tid, status='abandoned')
        goal.pop('itemDependencies')
        thread.pop('itemDependencies')
        node = self.advance('放下这条核查路径', '你明确放下原先利用物品核查的目标，也决定不再追查对应问题。', [goal], [thread])
        for field in ('goalLedger', 'threadLedger'):
            self.assertEqual(node['branchState'][field][-1]['itemDependencies'], [self.iid])
        outline = self.outline()
        self.assertFalse(outline['conflicts'])
        self.assertEqual(outline['goals'][-1]['status'], 'abandoned')
        self.assertEqual(outline['threads'][-1]['status'], 'abandoned')

    def test_transformed_successor_must_drop_destroyed_dependency_explicitly(self):
        self.establish()
        self.destroy()
        goal = self.goal(id=self.gid, status='transformed', successor='观察现场寻找另一条线索')
        goal.pop('itemDependencies')
        plan = self.result()['consequenceUpdate']
        plan['stateChanges'] = []
        plan['steps'][0]['usedItemIds'] = []
        plan['goalUpdates'] = [goal]
        with self.assertRaisesRegex(ValueError, '永久损毁'):
            rc.validate_plan(plan, {'A1': self.context['playerDirection']}, self.context)
        goal['itemDependencies'] = []
        self.advance('改换调查目标', '你放下原来的核查目标，改为观察现场寻找另一条线索。', [goal])
        successor = self.state['goalLedger'][-1]
        self.assertEqual(successor['previousGoalId'], self.gid)
        self.assertEqual(successor['itemDependencies'], [])
        self.assertEqual(self.outline()['goals'][-1]['status'], 'active')

    def test_unproven_destruction_and_forged_dependencies_stay_unknown(self):
        self.establish()
        self.destroy()
        nodes = copy.deepcopy(self.context['lineage'])
        nodes[-1]['consequenceUpdate']['stateChanges'] = []
        outline = self.outline(nodes)
        self.assertTrue(all(c['status'] == 'unknown' and c['evidence'] is None for c in outline['conflicts']))
        self.assertEqual(preparation(self.package, self.context['contract'], nodes, 'active')['readiness'], 'blocked_unknown')
        nodes = copy.deepcopy(self.context['lineage'])
        for field in ('goalLedger', 'threadLedger'):
            nodes[-1]['branchState'][field][-1]['itemDependencies'] = []
        outline = self.outline(nodes)
        self.assertEqual(outline['goals'][-1]['status'], 'unknown')
        self.assertEqual(outline['threads'][-1]['status'], 'unknown')
        self.assertFalse(outline['conflicts'])

    def test_http_roundtrip_preserves_item_conflicts_and_does_not_write(self):
        self.establish()
        node = self.destroy()
        before = self.read.database_path.read_bytes()
        with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=False)) as client, \
                patch.object(SessionStore, '__init__', side_effect=AssertionError('read cannot initialize writer')):
            url = f'/api/v1/sessions/{self.sid}/route-outline'
            response = client.get(url, params={'branch_id': node['id']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['outline']['conflicts'], self.outline()['conflicts'])
        self.assertEqual(before, self.read.database_path.read_bytes())

    def test_dependency_order_and_legacy_empty_field_do_not_fake_progress(self):
        self.establish()
        nodes = copy.deepcopy(self.context['lineage'])
        # Both existing and newly registered items are valid dependency IDs.
        second = 'item_dependency_remains'
        nodes[-1]['branchState']['derivedItems'].append(dict(id=second, name='留存碎屑', summary='道具碎屑'))
        for field in ('goalLedger', 'threadLedger'):
            nodes[-1]['branchState'][field][-1]['itemDependencies'] = [self.iid, second]
        child = copy.deepcopy(nodes[-1])
        child.update(id='reordered', parentId=nodes[-1]['id'])
        for field in ('goalLedger', 'threadLedger'):
            child['branchState'][field][-1]['itemDependencies'].reverse()
            child['branchState'][field][0]['itemDependencies'] = []
        result = monitor([*nodes, child], self.package, self.context['contract'], 'active')
        self.assertEqual(result['recent_turns'][-1]['changed'], [])


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformItemDependencies_' + case['package_id'],
                                                        (LongformItemDependencyTests,), {'case': case})))
    return suite
