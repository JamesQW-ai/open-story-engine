"""Result fidelity, permanent state and role goals across real store boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from open_story_engine import reader_consequences as rc
from open_story_engine import reader_actions as ra
from open_story_engine.api_narrative import PlayerNarrativePlanner, player_package
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements, validate_action_requirements
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import apply_branch_patch, create_contract, entry_node
from open_story_engine.llm import Completion

ROOT = Path(__file__).resolve().parents[1]
GU = 'character_e663361ab1c7'
LU = 'character_ae4cb42b9b49'
BODY = '你趁陆照临侧身之际扑了过去，将他压倒在地，扼住他的喉咙。他拼命挣扎，你手臂酸痛却没有松开。\n\n陆照临终于不再动弹。你俯下身确认他已经死亡，才慢慢松手，望着自己颤抖的手指。'


def observed(body):
    return {'events': [dict(id=f'O{i+1}', paragraphId=f'P{i+1}', quote=p, actor=GU, mode='actual', summary=p, changes=[], introduced=[]) for i, p in enumerate(body.split('\n\n'))]}


def checked(body):
    return [dict(id=e['id'], verdict='supported', reason='已核对原输入', stepIds=['S1'], changeIds=[], introductionIds=[]) for e in observed(body)['events']]


def authority(contract):
    return dict(decision='allow', issues=[],
                checks=[dict(stepId=step['id'], authorized=True, basis='player_input', quote=next(iter(contract['requirements'].values()))['summary'], reason='直接输入授权') for step in contract['steps']],
                stateChecks=[dict(changeId=c['id'], authorized=True, reason='步骤直接导致') for c in contract['stateChanges']])


def seal(result):
    body = result['narrativeText']
    return {**result, 'authorityReview': authority(result['consequenceUpdate']), 'reviewedNarrativeSha256': hashlib.sha256(body.encode()).hexdigest(), 'observedEvents': observed(body)['events'], 'eventChecks': checked(body)}


def scene_checked(body):
    return [dict(paragraphId=f'P{i+1}', playerDecision='authorized', background='none', sources=[], issue='')
            for i, _ in enumerate(body.split('\n\n'))]


def grounded(body, requirements=None, *, repair_count=0):
    from open_story_engine.reader_scene_review import grounding_claims, dialogue_units
    return {'checks': [dict(id=k, verdict='supported', kind='current', sources=[], scopeViolations=[], reason='当前授权动作')
                       for k in grounding_claims(body)],
            'knowledgeChecks': [dict(id=k, speakerId=GU, kind='current', verdict='supported', accessSources=[], missingEvidence=[], reason='当前对话') for k in dialogue_units(body)],
            'repairChecks': [dict(id=f'R{i+1}', verdict='resolved', paragraphIds=['P1'], reason='错误已移除') for i in range(repair_count)],
            'scopeChecks': [dict(id=k, verdict='satisfied', paragraphIds=['P1'], reason='当前正文完整执行要求')
                            for k in (requirements or {'A1': ''})]}


def plan(requirements, outcomes=None, goals=None):
    return dict(decision='ready', readingIntent='brief', requirements={k: {'mode': 'result', 'summary': v} for k, v in requirements.items()},
                method='根据当前条件落实行动', outcomes=outcomes or [], goalUpdates=goals or [],
                steps=[dict(id='S1', actorId=GU, action='；'.join(requirements.values()), requirementIds=list(requirements), authority='player', causeStepId=None)],
                introductions=dict(characters=[], items=[], locations=[]), stateChanges=[],
                scenePlan=dict(start='当前场景', outcome='行动完成', stop='等待玩家选择',
                               lengthReason='单次交互，无需展开其他动作', targetCjk=[80, 250],
                               beats=[dict(purpose='完整回应当前行动', stepIds=['S1'])],
                               knowledge=[], observationLimits=[]))


class ConsequenceTests(unittest.TestCase):
    def test_model_state_omits_repeated_ledger_but_keeps_permanent_and_current_facts(self):
        state = {'branchLedger': {'entries': [{'before': '重复快照'}]},
                 'readerEntityStates': {'item': {'完整性': '破碎'}},
                 'characterOutcomeStates': {LU: {'status': 'dead', 'permanence': 'permanent'}},
                 'goalLedger': [{'status': 'abandoned'}], 'playerLocationId': 'yard'}
        before = copy.deepcopy(state)
        result = rc.prompt_state(state)
        self.assertEqual(result, {k: v for k, v in before.items() if k != 'branchLedger'})
        self.assertEqual(state, before)

    def test_closing_phase_rejects_new_or_transformed_long_term_obligations(self):
        closing = {**self.context, 'closingIntent': {'intended_type': 'normal'},
                   'lineage': [self.root]}
        with patch('open_story_engine.reader_consequences._closing_phase', return_value=True):
            with self.assertRaisesRegex(ValueError, '不得建立新目标'):
                rc.validate_plan({**self.plan, 'goalUpdates': [
                    dict(id='new', title='收束后新目标', status='active', dependencies=[],
                         reason='收束中建立新目标')
                ]}, self.requirements, closing)
            with self.assertRaisesRegex(ValueError, '不得建立新目标'):
                rc.validate_plan({**self.plan, 'goalUpdates': [
                    dict(id=self.plan['goalUpdates'][0]['id'] if self.plan['goalUpdates'] else 'opening-goal-1',
                         title='确认当前目标', status='transformed', dependencies=[],
                         reason='收束中改换目标', successor='新的长期目标')
                ]}, self.requirements, closing)
            with self.assertRaisesRegex(ValueError, '不得建立新的长期剧情问题'):
                rc.validate_plan({**self.plan, 'threadUpdates': [
                    dict(id='new-1', title='收束后新问题', status='open', reason='收束中建立问题', stepIds=['S1'])
                ]}, self.requirements, closing)

    def setUp(self):
        self.package = player_package(load_runtime_story_package(ROOT / 'content/packages/taixu-relics-part1/0.1.2/package.json', lazy=True), GU)
        self.contract = create_contract(self.package, 'fixture', {'kind': 'source_character', 'sourceCharacterId': GU, 'entryPointId': 'entry_gu_trial'})
        self.root = entry_node(self.package, self.contract)
        self.context = dict(package=self.package, contract=self.contract, parent=self.root, lineage=[self.root], playerDirection='本回合杀死陆照临')
        self.requirements = action_requirements(self.context['playerDirection'])
        self.plan = plan(self.requirements, [dict(characterId=LU, status='dead', permanence='permanent', requirementId='A1', cause='被玩家扼死')])

    def reviewed_result(self):
        data = dict(issues=[], actions=[dict(id='A1', status='performed', summary='陆照临已死亡', paragraphId='P2')], checkedConsequences=True, outcomeEvidence=['P2'], goalEvidence=[], changeEvidence=[], introductionEvidence=[])
        reader = validate_action_requirements(data, self.requirements, BODY)
        update = rc.validate_review(data, self.plan, BODY, reader)
        return seal(dict(narrativeText=BODY, actionIntent={'input': self.context['playerDirection']}, consequenceUpdate=update, consequenceReview=rc.VERSION))

    def test_result_cannot_be_downgraded_to_blocked_and_evidence_is_required(self):
        data = dict(issues=[], actions=[dict(id='A1', status='blocked', summary='受到阻挡', paragraphId='P1')], checkedConsequences=True, outcomeEvidence=['P2'], goalEvidence=[], changeEvidence=[], introductionEvidence=[])
        reader = validate_action_requirements(data, self.requirements, BODY)
        with self.assertRaisesRegex(ValueError, '不能以尝试受阻'):
            rc.validate_review(data, self.plan, BODY, reader)
        self.plan['requirements']['A1']['mode'] = 'attempt'
        rc.validate_review(data, self.plan, BODY, reader)
        data['outcomeEvidence'] = ['P99']
        with self.assertRaises(ValueError):
            rc.validate_review(data, self.plan, BODY, reader)
        data['outcomeEvidence'] = []
        with self.assertRaises(ValueError):
            rc.validate_review(data, self.plan, BODY, reader)

    def test_death_is_permanent_and_unreviewed_or_edited_result_cannot_commit(self):
        for bad in (dict(characterId='unknown'), dict(permanence='temporary'), dict(requirementId='A9')):
            candidate = copy.deepcopy(self.plan)
            candidate['outcomes'][0].update(bad)
            with self.assertRaises(ValueError):
                rc.validate_plan(candidate, self.requirements, self.context)
        result = self.reviewed_result()
        state = rc.commit_consequences(self.context, self.root['branchState'], result, 'death-branch')
        self.assertEqual(state['characterOutcomeStates'][LU]['causeBranchId'], 'death-branch')
        self.assertEqual(self.root['branchState']['characterOutcomeStates'], {})
        context = {**self.context, 'parent': {**self.root, 'branchState': state}}
        repeated = rc.validate_plan(self.plan, self.requirements, context)
        self.assertEqual(repeated['outcomes'], [])
        for status in ('alive', 'departed'):
            candidate = copy.deepcopy(self.plan)
            candidate['outcomes'][0]['status'] = status
            with self.assertRaisesRegex(ValueError, '不可撤销'):
                rc.validate_plan(candidate, self.requirements, context)
        for result_change in ({'consequenceReview': None}, {'narrativeText': '你打算杀死他，但没有行动。'}):
            with self.assertRaises(ValueError):
                rc.commit_consequences(self.context, self.root['branchState'], {**result, **result_change}, 'bad')
        for patch_value in ({}, None):
            with self.assertRaises(ValueError):
                apply_branch_patch(self.package, state, {'characterOutcomeStates': patch_value}, self.root['sourceNodeRef'])
        with self.assertRaisesRegex(ValueError, '永久下线'):
            apply_branch_patch(self.package, state, {'characterLocationIds': {LU: 'location_open_gate'}}, self.root['sourceNodeRef'])
        directions = [dict(title='与陆照临同行', summary='询问计划'), dict(title='查看尸体留下的痕迹', summary='停在原地')]
        self.assertEqual(len(rc.filter_directions(directions, self.package, state)), 1)

    def test_role_goals_use_opening_knowledge_and_goal_changes_preserve_history(self):
        goals = rc.initial_goals(self.package, self.contract)
        self.assertTrue(goals)
        self.assertNotIn('父亲', str(goals))
        titles = set()
        for entry in self.package['story']['entryModel']['entryPoints']:
            contract = {**self.contract, 'entryPointId': entry['id']}
            titles.add(tuple(g['title'] for g in rc.initial_goals(self.package, contract)))
        self.assertEqual(len(titles), 3)
        result = self.reviewed_result()
        old = goals[0]
        overwrite = dict(id=old['id'], title='新的求生目标', status='active', dependencies=[], reason='目标改变', successor='')
        with self.assertRaisesRegex(ValueError, '不能覆盖旧目标历史'):
            rc.validate_plan(plan(self.requirements, goals=[overwrite]), self.requirements, self.context)
        result['consequenceUpdate']['goalUpdates'] = [dict(id=old['id'], title=old['title'], status='transformed', dependencies=[], reason='玩家放下试炼转为求生', successor='离开争端，寻找安全的落脚处', evidence=BODY.split('\n\n')[0])]
        state = rc.commit_consequences(self.context, self.root['branchState'], result, 'new-goal')
        self.assertEqual(state['goalLedger'][0]['title'], old['title'])
        self.assertEqual(state['goalLedger'][0]['status'], 'transformed')
        self.assertEqual(state['goalLedger'][-1]['status'], 'active')
        self.assertEqual(state['goalLedger'][-1]['previousGoalId'], old['id'])

    def test_ambiguous_exit_and_hard_conflicts_do_not_become_ready(self):
        for decision in ('clarification_needed', 'conflict'):
            result = rc.validate_plan({'decision': decision, 'message': '请明确是死亡还是永久离队'}, self.requirements, self.context)
            self.assertEqual(result['decision'], decision)
        candidate = copy.deepcopy(self.plan)
        candidate['requirements'] = {}
        with self.assertRaises(ValueError):
            rc.validate_plan(candidate, self.requirements, self.context)

    def test_permanent_departure_removes_presence_but_does_not_grant_items(self):
        self.plan['outcomes'][0]['status'] = 'departed'
        body = '陆照临与你说定，此后不再同行，随即转身离开。你留在原地，没有交换任何物品。'
        data = dict(checkedConsequences=True, outcomeEvidence=['P1'], goalEvidence=[], changeEvidence=[], introductionEvidence=[])
        reader = {'actions': [dict(id='A1', status='performed', requirement='永久离队')]}
        result = dict(narrativeText=body, consequenceReview=rc.VERSION,
                      consequenceUpdate=rc.validate_review(data, self.plan, body, reader))
        state = rc.commit_consequences(self.context, self.root['branchState'], seal(result), 'exit')
        self.assertNotIn(LU, state['characterLocationIds'])
        self.assertEqual(state['itemOwnerCharacterIds'], self.root['branchState']['itemOwnerCharacterIds'])
        context = {**self.context, 'parent': {**self.root, 'branchState': state}}
        self.plan['outcomes'][0]['status'] = 'alive'
        with self.assertRaises(ValueError):
            rc.validate_plan(self.plan, self.requirements, context)

    def test_no_movement_is_not_death_and_new_goals_drive_directions(self):
        body = '你低头看着陆照临，他倒在地上一动不动。'
        data = dict(checkedConsequences=True, outcomeEvidence=['P1'], goalEvidence=[], changeEvidence=[], introductionEvidence=[])
        reader = {'actions': [dict(id='A1', status='performed', requirement='杀死陆照临')]}
        events = observed(body)['events']
        events[0]['changes'] = [dict(entityId=LU, attribute='outcome', value='alive')]
        with self.assertRaisesRegex(ValueError, '未登记'):
            ra.validate_events({'eventChecks': checked(body)}, events, self.plan)
        state = {'freeTextProgress': 2, 'goalLedger': [
            dict(id='old', title='参加试炼', status='abandoned', source='player_branch'),
            dict(id='new', title='承担后果', status='active', source='player_branch')]}
        directions = rc.goal_directions(state)
        self.assertEqual(len(directions), 1)
        self.assertIn('承担后果', directions[0]['title'])
        self.assertNotIn('参加试炼', directions[0]['summary'])
        self.assertEqual(directions[0]['statePatch'], {'freeTextProgress': 3})
        state['goalLedger'][1]['title'] = '承担杀死陆照临的后果'
        state['characterOutcomeStates'] = {LU: {'status': 'dead', 'permanence': 'permanent'}}
        self.assertEqual(len(rc.filter_directions(rc.goal_directions(state), self.package, state)), 1)

    def test_evidence_repair_keeps_valid_prose_and_dependency_means_living_actor(self):
        action = '不再追查金屑'
        requirements = action_requirements(action)
        goals = rc.initial_goals(self.package, self.contract)
        update = dict(id=goals[0]['id'], title=goals[0]['title'], status='abandoned', dependencies=[], reason='你决定不再追查', successor='')
        contract = plan(requirements, goals=[update])
        body = '你把包着金屑的纸留在身上，不再追问它的来历。你决定放下这条线索，先留在原地。'
        review = dict(sceneChecks=scene_checked(body), finalState=rc.final_state_projection(self.root['branchState']), issues=[], actions=[dict(id='A1', status='performed', summary='你已放下追查金屑的目标', paragraphId='P1')], checkedConsequences=True, outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[])
        gateway = Mock(model='fixture')
        gateway.complete_text.return_value = Completion(body, '{}', [])
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', []) for x in (contract, authority(contract), observed(body), {**review, 'eventChecks': checked(body)}, grounded(body), {**review, 'goalEvidence': ['P1'], 'eventChecks': checked(body)})]
        context = {**self.context, 'playerDirection': action, 'characterDetails': []}
        selected = dict(id='custom', title=action, summary=action, isFreeText=True, statePatch={'freeTextProgress': 1})
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.root['branchState'])
        self.assertEqual(result['narrativeText'], body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 6)
        review_prompt = gateway.complete_json.call_args_list[3].args[0][1]['content']
        self.assertNotIn('expectedFinalState', review_prompt)
        extraction = json.loads(gateway.complete_json.call_args_list[2].args[0][1]['content'])
        for forbidden in ('input', 'requirements', 'resultContract', 'expectedFinalState'):
            self.assertNotIn(forbidden, extraction)
        self.assertTrue(all(set(entry) == {'entityId', 'attribute'} for entry in extraction['attributeVocabulary']))
        self.assertIn('本回合没有授权转场', gateway.complete_text.call_args.args[0][1]['content'])
        self.assertEqual(result['consequenceUpdate']['goalUpdates'][0]['evidence'], body)
        dead = rc.commit_consequences(self.context, self.root['branchState'], self.reviewed_result(), 'death')
        update.update(id='new', status='active', title='向陆照临当面问话', dependencies=[LU])
        with self.assertRaisesRegex(ValueError, '依赖永久下线'):
            rc.validate_plan(plan(requirements, goals=[update]), requirements, {**context, 'parent': {**self.root, 'branchState': dead}})

    def test_bad_review_reference_repairs_json_once_without_rewriting_prose(self):
        action = '留在原地等候'
        requirements = action_requirements(action)
        contract = plan(requirements)
        body = '你留在原地等候，没有交出木牌或纸包。'
        review = dict(sceneChecks=scene_checked(body), finalState=rc.final_state_projection(self.root['branchState']), issues=[],
                      actions=[dict(id='A1', status='performed', summary='留在原地等候', paragraphId='P1')],
                      checkedConsequences=True, outcomeEvidence=[], goalEvidence=[], changeEvidence=[], introductionEvidence=[],
                      eventChecks=checked(body))
        invalid = copy.deepcopy(review)
        invalid['eventChecks'][0]['changeIds'] = ['C999']
        gateway = Mock(model='fixture')
        gateway.complete_text.return_value = Completion(body, '{}', [])
        gateway.complete_json.side_effect = [Completion(json.dumps(x, ensure_ascii=False), '{}', [])
                                             for x in (contract, authority(contract), observed(body), invalid, grounded(body), review)]
        context = {**self.context, 'playerDirection': action, 'characterDetails': []}
        selected = dict(id='custom', title=action, summary=action, isFreeText=True, statePatch={'freeTextProgress': 1})
        result, audit = PlayerNarrativePlanner(gateway).plan(context, selected, self.root['branchState'])
        self.assertEqual(result['narrativeText'], body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(gateway.complete_json.call_count, 6)
        self.assertIn('C999', gateway.complete_json.call_args.args[0][-1]['content'])
        invalid['eventChecks'][0].update(verdict='unsupported', reason='玩家未授权')
        with self.assertRaises(ValueError) as caught:
            ra.validate_events(invalid, observed(body)['events'], contract)
        self.assertNotIsInstance(caught.exception, ra.ActionEvidenceError)

    def test_ambiguous_action_returns_question_without_body_or_saved_branch(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.2', 'entry_gu_trial', GU, identity_opening=True)
            sid, root = start['session']['id'], start['branch']
            gateway = Mock(model='fixture')
            gateway.complete_json.return_value = Completion(json.dumps({'decision': 'clarification_needed', 'message': '你希望陆照临死亡，还是永久离队？'}, ensure_ascii=False), '{}', [])
            gateway.__deepcopy__ = lambda memo: Mock(model='fixture', complete_json=gateway.complete_json, complete_text=gateway.complete_text)
            play._planner = PlayerNarrativePlanner(gateway)
            result = play.continue_turn(sid, root['id'], text='让陆照临永久下线', request_id='ambiguous')
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(result['kind'], 'clarification_needed')
            self.assertIn('还是永久离队', result['reason'])
            gateway.complete_text.assert_not_called()
            with read.store() as store:
                self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM branch_nodes').fetchone()[0], 1)

    def test_incidental_transfer_and_travel_cannot_be_saved_as_unchanged(self):
        state = self.root['branchState']
        for field, value in (('playerLocationId', 'location_open_gate'), ('itemOwnerCharacterIds', {})):
            projection = rc.final_state_projection(state)
            projection[field] = value
            with self.assertRaisesRegex(ValueError, '未经授权'):
                rc.validate_final_state({'finalState': projection}, state)

    def test_explicit_player_departure_requires_location_state_change(self):
        requirements = action_requirements('放弃父亲和空灯追查，拒绝救人并离开')
        candidate = plan(requirements)
        with self.assertRaisesRegex(ValueError, '没有登记地点变化'):
            rc.validate_plan(candidate, requirements, self.context)

        current = self.root['branchState']['playerLocationId']
        destination = next(location['id'] for location in self.package['locations']
                           if location['id'] != current)
        candidate['stateChanges'] = [dict(id='C1', entityId=GU, attribute='locationId',
                                          before=current, value=destination, stepId='S1')]
        rc.validate_plan(candidate, requirements, self.context)

        staying_requirements = action_requirements('留在原地，不离开当前场景')
        rc.validate_plan(plan(staying_requirements), staying_requirements, self.context)

        npc_requirements = action_requirements('让陆照临离开当前地点')
        rc.validate_plan(plan(npc_requirements), npc_requirements, self.context)

    def test_unrequested_player_action_rejected_without_keyword_matching(self):
        body = '你早已俯身伏地，等他发落。'
        events = observed(body)['events']
        events[0]['changes'] = [dict(entityId=GU, attribute='姿态', value='伏地')]
        with self.assertRaisesRegex(ValueError, '未登记'):
            ra.validate_events({'eventChecks': checked(body)}, events, self.plan)
        # The same words inside an NPC command are not an actual state change.
        events[0].update(mode='speech', changes=[])
        ra.validate_events({'eventChecks': checked(body)}, events, self.plan)

    def test_departure_evidence_can_span_refusal_and_exit(self):
        self.plan['outcomes'][0]['status'] = 'departed'
        body = '“此后不会与你结伴。”你说。\n\n陆照临应下，转身离开，消失在石径尽头。'
        reader = {'actions': [dict(id='A1', status='performed', requirement='永久离队')]}
        data = dict(checkedConsequences=True, outcomeEvidence=[['P1', 'P2']], goalEvidence=[], changeEvidence=[], introductionEvidence=[])
        update = rc.validate_review(data, self.plan, body, reader)
        self.assertEqual(update['outcomes'][0]['evidence'], body)
        state = rc.commit_consequences(self.context, self.root['branchState'], seal(dict(narrativeText=body, consequenceUpdate=update, consequenceReview=rc.VERSION)), 'exit')
        self.assertNotIn(LU, state['characterLocationIds'])

    def test_five_followups_restore_and_sibling_do_not_revive_or_share_outcomes(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.2', 'entry_gu_trial', GU, identity_opening=True)
            sid, root = start['session']['id'], start['branch']
            self.assertNotEqual(read.journey(sid, root['id'])['goal'], '探索这段故事，走到属于你的结局')
            responses = []
            texts = []
            def enqueue(action, body, outcomes):
                req = action_requirements(action)
                responses.append(Completion(json.dumps(plan(req, outcomes), ensure_ascii=False), '{}', []))
                responses.append(Completion(json.dumps(authority(plan(req, outcomes)), ensure_ascii=False), '{}', []))
                responses.append(Completion(json.dumps(observed(body), ensure_ascii=False), '{}', []))
                responses.append(Completion(json.dumps(dict(sceneChecks=scene_checked(body), eventChecks=checked(body), finalState=rc.final_state_projection(self.root['branchState']), issues=[], actions=[dict(id=k, status='performed', summary='已落实本回合行动', paragraphId='P1') for k in req], checkedConsequences=True, outcomeEvidence=['P2'] if outcomes else [], goalEvidence=[], changeEvidence=[], introductionEvidence=[]), ensure_ascii=False), '{}', []))
                texts.append(Completion(body, '{}', []))
            gateway = Mock(model='fixture')
            gateway.complete_text.side_effect = lambda *args: texts.pop(0)
            def complete_json(messages, *args):
                from open_story_engine.prompts import render_prompt
                if messages[0]['content'] == render_prompt('reader.scene_expand'):
                    return Completion('{"insertions":[]}', '{}', [])
                if messages[0]['content'] == render_prompt('reader.scene_grounding'):
                    data = json.loads(messages[1]['content'])
                    return Completion(json.dumps(grounded('\n\n'.join(data['draft'].values()), data['requirements'])), '{}', [])
                if messages[0]['content'] == render_prompt('reader.choices'):
                    menu = {'choices': [{'title': '留在原地等候', 'action': '我留在原地等候回应，暂时不移动或交出物品。',
                                         'paragraphIds': ['P1'], 'interactWith': []}]}
                    return Completion(json.dumps(menu, ensure_ascii=False), '{}', [])
                return responses.pop(0)
            gateway.complete_json.side_effect = complete_json
            gateway.__deepcopy__ = lambda memo: Mock(model='fixture', complete_text=gateway.complete_text, complete_json=gateway.complete_json)
            play._planner = PlayerNarrativePlanner(gateway)
            enqueue('本回合杀死陆照临', BODY, self.plan['outcomes'])
            killed = play.continue_turn(sid, root['id'], text='本回合杀死陆照临', request_id='kill')['branch']
            cause = killed['id']
            current = killed
            for i in range(5):
                action = '留在原地观察周围'
                enqueue(action, '你留在原地，警惕地看着周围，手臂还在发酸。没有人向你开口，你也没有再迈出一步。', [])
                current = play.continue_turn(sid, current['id'], text=action, request_id='after-' + str(i))['branch']
                with read.store() as store:
                    saved = store.branch(sid, current['id'])
                    history = store.lineage(sid, current['id'])
                self.assertEqual(saved['branchState']['characterOutcomeStates'][LU]['causeBranchId'], cause)
                self.assertEqual(saved['branchState']['characterOutcomeStates'][LU]['status'], 'dead')
                self.assertEqual(len(saved['readerChoices']), 1)
                from open_story_engine.api_turn_drafts import visible_choices
                self.assertEqual(len(visible_choices(saved, self.package, self.contract, history)), 1)
                candidate = plan(action_requirements(action))
                candidate['steps'].append(dict(id='S2', actorId=LU, action='回答并继续带路',
                                               requirementIds=['A1'], authority='reaction', causeStepId='S1'))
                with self.assertRaisesRegex(ValueError, '永久下线'):
                    ra.validate_plan(candidate, {**self.context, 'parent': saved})
            # A restart reuses persisted state, not the planner's process memory.
            play.drafts.close()
            restored = PlayService(read, Path(tmp))
            self.addCleanup(restored.drafts.close)
            self.assertEqual(read.journey(sid, current['id'])['goals'], read.journey(sid, killed['id'])['goals'])
            with read.store() as store:
                restored_branch = store.branch(sid, current['id'])
                history = store.lineage(sid, current['id'])
            self.assertEqual(len(visible_choices(restored_branch, self.package, self.contract, history)), 1)
            with self.assertRaisesRegex(ValueError, '永久下线'):
                ra.validate_plan(candidate, {**self.context, 'parent': restored_branch})
            restored._planner = PlayerNarrativePlanner(gateway)
            enqueue('留在原地观察周围', '你留在原地看着陆照临，等他开口。你没有挪动手里的木牌，也没有触碰包着金屑的纸片。', [])
            sibling = restored.continue_turn(sid, root['id'], text='留在原地观察周围', request_id='sibling')['branch']
            self.assertEqual(sibling['branchState']['characterOutcomeStates'], {})
            self.assertEqual(responses, [])
