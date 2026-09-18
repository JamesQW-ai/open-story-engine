"""Permanent item consequences on every eligible official long novel."""
import copy
import hashlib
from contextlib import closing
import unittest

from open_story_engine import item_lifecycle as items, reader_actions as ra, reader_consequences as rc
from open_story_engine.cocreation import apply_branch_patch, validate_branch_additions
from open_story_engine.reader_choices import choice_context, validate_choices, restored_choices
from open_story_engine.scene_library import SceneLibrary
from open_story_engine.storage import SessionStore
from test_support.longform import longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformItemDestructionTests(unittest.TestCase):
    def setUp(self):
        LongformRepairFallbackTests.setUp(self)
        _, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        self.context = dict(package=snapshot.package, contract=snapshot.story_contract,
                            parent=snapshot.history[-1], lineage=snapshot.history)
        self.state = self.context['parent']['branchState']
        self.package = snapshot.package
        self.item = next((i for i in self.package['items']
                          if self.state['itemOwnerCharacterIds'].get(i['id']) == self.cid), self.package['items'][0])
        self.iid = self.item['id']
        self.context['playerDirection'] = '彻底毁掉' + self.item['name']

    def result(self):
        action = self.context['playerDirection']
        body = '你将' + self.item['name'] + '彻底毁去。原物已不复存在，也不能再修复。'
        plan = dict(decision='ready', requirements={'A1': {'mode': 'result', 'summary': action}},
                    method='执行明确的损毁行动', outcomes=[], goalUpdates=[],
                    introductions=dict(characters=[], items=[], locations=[]),
                    steps=[dict(id='S1', actorId=self.cid, action=action, requirementIds=['A1'],
                                authority='player', causeStepId=None, usedItemIds=[self.iid])],
                    stateChanges=[dict(id='C1', entityId=self.iid, attribute=items.ATTRIBUTE, before=None,
                                       value=True, stepId='S1', reason='玩家明确要求彻底毁去', evidence=body)])
        return dict(narrativeText=body, consequenceUpdate=plan, consequenceReview=rc.VERSION,
                    actionIntent={'input': action}, reviewedNarrativeSha256=hashlib.sha256(body.encode()).hexdigest(),
                    authorityReview=dict(decision='allow', issues=[],
                        stateChecks=[dict(changeId='C1', authorized=True)],
                        checks=[dict(stepId='S1', authorized=True, basis='player_input', quote=action)]),
                    observedEvents=[dict(id='O1', paragraphId='P1', actor=self.cid, mode='actual', summary=body,
                        usedItemIds=[self.iid], introduced=[],
                        changes=[dict(entityId=self.iid, attribute=items.ATTRIBUTE, value=True)])],
                    eventChecks=[dict(id='O1', verdict='supported', stepIds=['S1'], changeIds=['C1'], introductionIds=[])])

    def node(self):
        result = self.result()
        state = rc.commit_consequences(self.context, self.state, result, 'destroyed-branch')
        return {**self.context['parent'], **result, 'id': 'destroyed-branch', 'branchState': state,
                'parentId': self.parent, 'sequence': 1, 'selectedDirectionId': 'item-action',
                'selectedDirection': {'id': 'item-action', 'isFreeText': True, 'statePatch': {}},
                'playerDirection': self.context['playerDirection']}

    def events(self, result, state=None):
        events = ra.validate_observations({'events': result['observedEvents']}, result['narrativeText'])
        return ra.validate_events(result, events, result['consequenceUpdate'],
                                  self.state if state is None else state, self.package)

    def test_commit_clears_positions_preserves_cause_and_roundtrips_without_mutating_parent(self):
        before = copy.deepcopy(self.state)
        node = self.node()
        state = node['branchState']
        self.assertTrue(items.destroyed(state, self.iid))
        self.assertNotIn(self.iid, state['itemOwnerCharacterIds'])
        self.assertNotIn(self.iid, state.get('itemLocationIds', {}))
        entries = [e for e in state['branchLedger']['entries'] if e['entityId'] == 'state:readerEntityStates']
        self.assertEqual(entries[-1]['source']['ref'], node['id'])
        self.assertIs(entries[-1]['after'][self.iid][items.ATTRIBUTE], True)
        self.assertEqual(self.state, before)
        with closing(SessionStore(str(self.read.database_path))) as store:
            store.append_branch(self.sid, self.parent, node)
        with self.read.store() as reopened:
            lineage = reopened.lineage(self.sid, node['id'])
            original = reopened.lineage(self.sid, self.parent)[-1]
        self.assertEqual(lineage[-1]['branchState'], state)
        self.assertEqual(lineage[-1]['consequenceUpdate']['stateChanges'][0]['evidence'], node['narrativeText'])
        self.assertEqual(original['branchState'], before)
        context = {**self.context, 'parent': lineage[-1], 'lineage': lineage}
        self.assertIs(rc.planning_context(context)['state']['readerEntityStates'][self.iid][items.ATTRIBUTE], True)
        wait = self.result()['consequenceUpdate']
        wait['steps'][0]['usedItemIds'] = []
        wait['stateChanges'] = []
        child = ra.project(state, wait, self.package)
        self.assertTrue(items.destroyed(child, self.iid))
        self.assertIn(self.item['name'] + '已永久损毁', rc.public_summary(node['consequenceUpdate'], self.package))

    def test_terminal_marker_has_strict_type_and_cannot_apply_to_people(self):
        for value in (False, None, 1, 'true'):
            with self.subTest(value=value):
                plan = self.result()['consequenceUpdate']
                plan['stateChanges'][0]['value'] = value
                with self.assertRaises(ValueError):
                    ra.validate_plan(plan, self.context)
        plan = self.result()['consequenceUpdate']
        plan['stateChanges'][0]['entityId'] = self.cid
        with self.assertRaisesRegex(ValueError, '只允许对道具'):
            ra.validate_plan(plan, self.context)

    def test_destroyed_original_cannot_be_restored_transferred_or_used_in_descendants(self):
        state = self.node()['branchState']
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': state}}
        for attribute, value in ((items.ATTRIBUTE, False), (items.ATTRIBUTE, None),
                                 ('ownerCharacterId', self.cid), ('locationId', state['playerLocationId']), ('完整性', '完整')):
            plan = self.result()['consequenceUpdate']
            plan['steps'][0]['usedItemIds'] = []
            plan['stateChanges'][0].update(attribute=attribute, value=value,
                before=ra.value_at(state, self.iid, attribute, 'item'))
            with self.subTest(attribute=attribute, value=value):
                with self.assertRaises(ValueError):
                    ra.validate_plan(plan, context)
                with self.assertRaises(ValueError):
                    ra.project(state, plan, self.package)
        plan['stateChanges'] = []
        plan['steps'][0]['usedItemIds'] = [self.iid]
        with self.assertRaisesRegex(ValueError, '不能使用'):
            ra.validate_plan(plan, context)
        for patch in ({'readerEntityStates': {}}, {'itemOwnerCharacterIds': {self.iid: self.cid}}):
            with self.assertRaises(ValueError):
                apply_branch_patch(self.package, state, patch, 'item-test')
        for attributes in ({'完整性': '完整'}, {items.ATTRIBUTE: False}):
            with self.assertRaisesRegex(ValueError, '行动契约'):
                validate_branch_additions(self.package, state, dict(changes=[dict(kind='item', entityId=self.iid,
                    summary='恢复原物', attributes=attributes)]))

    def test_destroying_step_can_use_item_but_later_steps_cannot(self):
        plan = self.result()['consequenceUpdate']
        ra.validate_plan(plan, self.context)
        plan['steps'].append(dict(plan['steps'][0], id='S2'))
        with self.assertRaisesRegex(ValueError, '不能使用'):
            ra.validate_plan(plan, self.context)
        plan['steps'][1]['usedItemIds'] = []
        ra.validate_plan(plan, self.context)
        plan['stateChanges'].append(dict(id='C2', entityId=self.iid, attribute='ownerCharacterId',
            before=self.state['itemOwnerCharacterIds'].get(self.iid), value=self.cid, stepId='S2'))
        with self.assertRaisesRegex(ValueError, '同一回合'):
            ra.validate_plan(plan, self.context)
        plan['stateChanges'].pop()
        for uses in (None, self.iid, [self.cid], ['item_unregistered']):
            plan['steps'][0]['usedItemIds'] = uses
            with self.assertRaisesRegex(ValueError, '已登记道具'):
                ra.validate_plan(plan, self.context)

    def test_commit_requires_actual_observed_and_reviewed_destruction(self):
        for mode in ('speech', 'intention', 'hypothetical', 'recollection', 'background'):
            result = self.result()
            result['observedEvents'][0]['mode'] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, '实际正文事件'):
                rc.commit_consequences(self.context, self.state, result, 'bad')
        for missing in ('observedEvents', 'eventChecks', 'authorityReview', 'reviewedNarrativeSha256'):
            result = self.result()
            result.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                rc.commit_consequences(self.context, self.state, result, 'bad')
        result = self.result()
        result['consequenceUpdate']['stateChanges'][0]['evidence'] = '不在当前正文内的损毁断言'
        with self.assertRaisesRegex(ValueError, '结果证据'):
            rc.commit_consequences(self.context, self.state, result, 'bad')
        for value in (False, 1, 'true'):
            result = self.result()
            result['observedEvents'][0]['changes'][0]['value'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '布尔 true'):
                self.events(result)

    def test_independent_observer_catches_unplanned_damage_and_later_reuse(self):
        result = self.result()
        result['consequenceUpdate']['stateChanges'] = []
        result['eventChecks'][0]['changeIds'] = []
        with self.assertRaisesRegex(ValueError, '未登记'):
            self.events(result)
        result = self.result()
        body = '你再次拿起原物，把它作为工具使用。'
        result['narrativeText'] += '\n\n' + body
        result['observedEvents'].append(dict(result['observedEvents'][0], id='O2', paragraphId='P2',
                                             summary=body, changes=[]))
        result['eventChecks'].append(dict(result['eventChecks'][0], id='O2', changeIds=[]))
        with self.assertRaisesRegex(ValueError, '不能使用'):
            self.events(result)
        result['observedEvents'][1].update(mode='recollection', usedItemIds=[])
        self.events(result)

    def test_ordinary_damage_and_unknown_are_not_permanent_and_remains_need_new_identity(self):
        plan = self.result()['consequenceUpdate']
        plan['stateChanges'][0].update(attribute='完整性', value='破碎')
        state = ra.project(self.state, plan, self.package)
        self.assertFalse(items.destroyed(state, self.iid))
        plan['stateChanges'][0].update(before='破碎', value='已修复')
        repaired = ra.project(state, plan, self.package)
        self.assertEqual(repaired['readerEntityStates'][self.iid]['完整性'], '已修复')
        self.assertFalse(items.destroyed({}, self.iid))
        state = self.node()['branchState']
        plan['stateChanges'] = [dict(id='C2', entityId='item_remains', attribute='ownerCharacterId',
                                    before=None, value=self.cid, stepId='S1')]
        plan['steps'][0]['usedItemIds'] = []
        plan['introductions']['items'] = [dict(id='item_remains', name=self.item['name'] + '残余碎屑',
                                             summary='原物损毁后留下的碎屑', sourceStepId='S1')]
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': state}}
        ra.validate_plan(plan, context)
        projected = ra.project(state, plan, self.package)
        self.assertTrue(items.destroyed(projected, self.iid))
        self.assertEqual(projected['itemOwnerCharacterIds']['item_remains'], self.cid)

    def test_choices_and_restored_menus_recheck_item_dependencies(self):
        node = self.node()
        context = choice_context(self.package, self.context['contract'], self.context['lineage'], node)
        public = next(i for i in context['items'] if i['id'] == self.iid)
        self.assertIs(public['state'][items.ATTRIBUTE], True)
        option = dict(title='检查损毁痕迹', action='我留在原地检查损毁造成的痕迹。', paragraphIds=['P1'],
                      interactWith=[], mentionOnly=[], useItems=[])
        choices = validate_choices({'choices': [option]}, context, self.package)
        self.assertEqual(restored_choices(choices, context, self.package), choices)
        usable = copy.deepcopy(context)
        next(i for i in usable['items'] if i['id'] == self.iid)['state'] = {}
        option.update(title='使用随身物品', action='我使用手里的物品，完成眼前已商定的动作。', useItems=[self.iid])
        saved = validate_choices({'choices': [option]}, usable, self.package)
        self.assertEqual(saved[0]['dependencies']['useItems'], [self.iid])
        self.assertEqual(restored_choices(saved, usable, self.package), saved)
        self.assertEqual(restored_choices(saved, context, self.package), [])
        for uses in ([self.iid], ['item_hidden'], None):
            with self.subTest(uses=uses), self.assertRaises(ValueError):
                validate_choices({'choices': [dict(option, useItems=uses)]}, context, self.package)

    def test_art_requires_explicit_destroyed_state_and_environment_remains_compatible(self):
        node = self.node()
        card = dict(location_id=node['branchState']['playerLocationId'], characters={}, required_state={},
                    review_status='approved', source_evidence=[node['narrativeText']])
        self.assertTrue(SceneLibrary.compatible(card, node))
        node['branchState']['readerEntityStates'][self.iid]['颜色'] = '黑'
        card['required_state'] = {'readerEntityStates': {self.iid: {'颜色': '黑'}}}
        self.assertFalse(SceneLibrary.compatible(card, node))
        card['required_state']['readerEntityStates'][self.iid][items.ATTRIBUTE] = True
        self.assertTrue(SceneLibrary.compatible(card, node))
        before = dict(node, branchState=self.state)
        self.assertFalse(SceneLibrary.compatible(card, before))
        for malformed in (None, [], 'unknown'):
            bad = dict(node, branchState={**node['branchState'], 'readerEntityStates': malformed})
            self.assertFalse(SceneLibrary.compatible(card, bad))


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformItemDestruction_' + case['package_id'],
                                                        (LongformItemDestructionTests,), {'case': case})))
    return suite
