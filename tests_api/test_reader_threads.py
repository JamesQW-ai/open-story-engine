"""Public thread lifecycle, evidence gates and persisted branch isolation."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from open_story_engine import reader_consequences as rc, reader_threads as rt
from open_story_engine.api_narrative import PlayerNarrativePlanner, player_package
from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_reader_quality import action_requirements
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import apply_branch_patch, create_contract, entry_node
from open_story_engine.llm import Completion
from open_story_engine.prompts import render_prompt
from open_story_engine.reader_choices import choice_context, context_digest
from tests_api.test_reader_consequences import (
    ROOT, GU, authority, checked, grounded, observed, plan, scene_checked, seal,
)


def update(tid='new-1', title='敲门的人是谁', status='open'):
    return dict(id=tid, title=title, status=status, reason='本回合明确交代', stepIds=['S1'])


class ReaderThreadTests(unittest.TestCase):
    def setUp(self):
        self.package = player_package(load_runtime_story_package(
            ROOT / 'content/packages/taixu-relics-part1/0.1.2/package.json', lazy=True), GU)
        self.contract = create_contract(self.package, 'fixture', dict(
            kind='source_character', sourceCharacterId=GU, entryPointId='entry_gu_trial'))
        self.root = entry_node(self.package, self.contract)
        self.context = dict(package=self.package, contract=self.contract, parent=self.root,
                            lineage=[self.root], playerDirection='记下敲门者身份尚未确认')
        self.requirements = action_requirements(self.context['playerDirection'])
        self.plan = {**plan(self.requirements), 'threadUpdates': [update()]}

    def reviewed(self, body='你记下尚未确认的问题：敲门的人是谁。'):
        review = dict(checkedConsequences=True, outcomeEvidence=[], goalEvidence=[],
                      changeEvidence=[], introductionEvidence=[], threadEvidence=['P1'])
        validated = rc.validate_plan(self.plan, self.requirements, self.context)
        reviewed = rc.validate_review(review, validated, body,
                                      {'actions': [dict(id='A1', status='performed')]})
        return seal(dict(narrativeText=body, consequenceReview=rc.VERSION, consequenceUpdate=reviewed))

    def test_opening_and_legacy_unknown_are_read_only_and_do_not_infer_from_goals_or_menus(self):
        for entry in self.package['story']['entryModel']['entryPoints']:
            contract = {**self.contract, 'entryPointId': entry['id']}
            initial = rt.initial_threads(self.package, contract)
            self.assertEqual([t['title'] for t in initial], entry['openingThreads'])
            self.assertTrue(all(t['status'] == 'open' for t in initial))
        self.assertTrue(self.root['branchState'][rt.STATE_KEY])
        state = copy.deepcopy(self.root['branchState'])
        state.pop(rt.STATE_KEY)
        state.update(goalLedger=[dict(id='old', status='completed')], openThreads=['选择下一方向'],
                     derivedClues=[dict(id='clue', name='某个已经知道的事实')])
        before = copy.deepcopy(state)
        legacy = rt.threads_for(self.package, self.contract, state)
        self.assertTrue(all(t['status'] == 'unknown' and not t['evidence'] for t in legacy))
        self.assertNotIn('选择下一方向', str(legacy))
        self.assertEqual(state, before)
        legacy[0]['status'] = 'resolved'
        self.assertEqual(rt.threads_for(self.package, self.contract, state)[0]['status'], 'unknown')
        for status in ('open', 'resolved', 'abandoned'):
            candidate = {**self.plan, 'threadUpdates': [update(legacy[0]['id'], legacy[0]['title'], status)]}
            rc.validate_plan(candidate, self.requirements, {**self.context, 'parent': {**self.root, 'branchState': state}})

    def test_invalid_updates_and_reopening_closed_threads_are_rejected(self):
        opening = self.root['branchState'][rt.STATE_KEY][0]
        cases = [None, {}, [update(tid='missing')], [update(), update()],
                 [update(status='resolved')], [update(status='unknown')],
                 [dict(update(), stepIds=['S99'])], [dict(update(), reason=' ')],
                 [update(tid=opening['id'], title='改写旧问题')],
                 [update(tid=opening['id'], title=opening['title'])],
                 [update(title=opening['title'])]]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rc.validate_plan({**self.plan, 'threadUpdates': bad}, self.requirements, self.context)
        for closed in ('resolved', 'abandoned'):
            parent = copy.deepcopy(self.root)
            parent['branchState'][rt.STATE_KEY][0]['status'] = closed
            for status in ('open', 'resolved', 'abandoned'):
                with self.subTest(closed=closed, status=status), self.assertRaisesRegex(ValueError, '已结束'):
                    rc.validate_plan({**self.plan, 'threadUpdates': [update(opening['id'], opening['title'], status)]},
                                     self.requirements, {**self.context, 'parent': parent})

    def test_priority_and_recovery_window_are_explicit_and_lineage_bound(self):
        opening = self.root['branchState'][rt.STATE_KEY][0]
        candidate = update(opening['id'], opening['title'])
        candidate.update(priority='high', recoveryWindow='near')
        validated = rc.validate_plan({**self.plan, 'threadUpdates': [candidate]}, self.requirements, self.context)
        reviewed = rc.validate_review(
            dict(checkedConsequences=True, outcomeEvidence=[], goalEvidence=[], changeEvidence=[],
                 introductionEvidence=[], threadEvidence=['P1']), validated,
            '你确认敲门者的问题仍待近期核实。', {'actions': [dict(id='A1', status='performed')]})
        state = rc.commit_consequences(self.context, self.root['branchState'],
                                       seal(dict(narrativeText='你确认敲门者的问题仍待近期核实。',
                                                 consequenceReview=rc.VERSION, consequenceUpdate=reviewed)), 'priority-branch')
        stored = next(item for item in state[rt.STATE_KEY] if item['id'] == opening['id'])
        self.assertEqual((stored['priority'], stored['recoveryWindow']), ('high', 'near'))
        with self.assertRaisesRegex(ValueError, '优先级'):
            rc.validate_plan({**self.plan, 'threadUpdates': [dict(candidate, priority='urgent')]},
                             self.requirements, self.context)
        with self.assertRaisesRegex(ValueError, '回收窗口'):
            rc.validate_plan({**self.plan, 'threadUpdates': [dict(candidate, recoveryWindow='someday')]},
                             self.requirements, self.context)

    def test_missing_or_forged_evidence_cannot_commit_and_raw_patch_cannot_bypass(self):
        body = '你记下敲门者身份仍然不明。'
        for evidence in (None, [], ['P99'], ['P1', 'P1']):
            data = dict(checkedConsequences=True, outcomeEvidence=[], goalEvidence=[],
                        changeEvidence=[], introductionEvidence=[], threadEvidence=evidence)
            with self.subTest(evidence=evidence), self.assertRaises(rc.ConsequenceEvidenceError):
                rc.validate_review(data, self.plan, body, {'actions': []})
        result = self.reviewed()
        for evidence in ('正文没有这句话', '', ' '):
            result['consequenceUpdate']['threadUpdates'][0]['evidence'] = evidence
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, '证据与提交正文不符'):
                rc.commit_consequences(self.context, self.root['branchState'], result, 'bad')
        with self.assertRaises(ValueError):
            apply_branch_patch(self.package, self.root['branchState'], {rt.STATE_KEY: []}, self.root['sourceNodeRef'])
        candidate = copy.deepcopy(self.plan)
        candidate['stateChanges'] = [dict(id='C1', entityId=GU, attribute=rt.STATE_KEY,
                                          before=None, value='resolved', stepId='S1')]
        with self.assertRaises(ValueError):
            rc.validate_plan(candidate, self.requirements, self.context)

    def test_reviewed_update_keeps_history_and_invalidates_direction_context(self):
        state = rc.commit_consequences(self.context, self.root['branchState'], self.reviewed(), 'left')
        thread = state[rt.STATE_KEY][-1]
        self.assertEqual((thread['status'], thread['openedBranchId'], thread['causeBranchId']), ('open', 'left', 'left'))
        self.assertNotEqual(thread['id'], 'new-1')
        self.assertEqual(state[rt.STATE_KEY][:-1], self.root['branchState'][rt.STATE_KEY])
        self.assertNotIn(thread, self.root['branchState'][rt.STATE_KEY])
        root = dict(self.root, sequence=0)
        before = choice_context(self.package, self.contract, [root], self.root)
        after = choice_context(self.package, self.contract, [root], {**self.root, 'branchState': state})
        self.assertNotEqual(context_digest(before), context_digest(after))
        self.assertEqual(after['threads'][-1]['title'], thread['title'])
        self.assertEqual(rc.planning_context({**self.context, 'parent': {**self.root, 'branchState': state}})['threads'][-1], thread)

    def test_five_turns_restore_and_sibling_keep_separate_thread_histories(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.2', 'entry_gu_trial', GU, identity_opening=True)
            sid, root = start['session']['id'], start['branch']
            gateway = Mock(model='fixture')
            responses, texts = [], []

            def complete_json(messages, *args):
                if messages[0]['content'] == render_prompt('reader.scene_grounding'):
                    payload = json.loads(messages[1]['content'])
                    return Completion(json.dumps(grounded('\n\n'.join(payload['draft'].values()), payload['requirements'])), '{}', [])
                if messages[0]['content'] == render_prompt('reader.choices'):
                    return Completion(json.dumps({'choices': [dict(title='留在原地等候',
                        action='我留在原地等候回应，暂时不移动或交出物品。', paragraphIds=['P1'], interactWith=[])]}), '{}', [])
                return responses.pop(0)

            gateway.complete_json.side_effect = complete_json
            gateway.complete_text.side_effect = lambda *args: texts.pop(0)
            gateway.__deepcopy__ = lambda memo: Mock(model='fixture', complete_text=gateway.complete_text, complete_json=gateway.complete_json)
            play._planner = PlayerNarrativePlanner(gateway)

            def advance(parent, action, body, updates, request):
                req = action_requirements(action)
                contract = dict(plan(req), threadUpdates=updates)
                review = dict(sceneChecks=scene_checked(body), eventChecks=checked(body),
                              finalState=rc.final_state_projection(parent['branchState']), issues=[],
                              actions=[dict(id=k, status='performed', summary=action, paragraphId='P1') for k in req],
                              checkedConsequences=True, outcomeEvidence=[], goalEvidence=[],
                              changeEvidence=[], introductionEvidence=[], threadEvidence=['P1'] * len(updates))
                responses.extend(Completion(json.dumps(x, ensure_ascii=False), '{}', [])
                                 for x in (contract, authority(contract), observed(body), review))
                texts.append(Completion(body, '{}', []))
                return play.continue_turn(sid, parent['id'], text=action, request_id=request)['branch']

            first = advance(root, '记下敲门者身份尚未确认', '你记下这个尚未回答的问题：敲门的人是谁。', [update()], 'first')
            tid = first['branchState'][rt.STATE_KEY][-1]['id']
            second = advance(first, '询问是谁敲门', '你询问敲门的是谁，回答只有一句“不知道”。身份仍未确认，你留在原地。', [], 'unknown')
            self.assertEqual(second['branchState'][rt.STATE_KEY][-1]['status'], 'open')
            third = advance(second, '确认刚才敲门的就是自己', '你确认刚才是自己敲的门，敲门者身份的问题已经有了答案。', [update(tid, status='resolved')], 'resolved')
            opening = root['branchState'][rt.STATE_KEY][0]
            fourth = advance(third, '明确放下这个开局问题', '你明确决定不再追查“' + opening['title'] + '”，将这个问题放下。',
                             [update(opening['id'], opening['title'], 'abandoned')], 'abandoned')
            fifth = advance(fourth, '留在原地等待', '你仍留在原地等待，刚才的问题没有再提起，也没有新的答案。', [], 'wait')
            self.assertEqual(gateway.complete_text.call_count, 5)
            # Planning, authority, observation, review, grounding, choices: no extra call for threads.
            self.assertEqual(gateway.complete_json.call_count, 30)
            self.assertEqual(responses, [])
            play.drafts.close()
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            play._planner = PlayerNarrativePlanner(gateway)
            journal = read.journey(sid, fifth['id'])
            from open_story_engine.api_turn_drafts import visible_choices
            with read.store() as store:
                saved = store.branch(sid, fifth['id'])
                self.assertEqual(len(visible_choices(saved, self.package, self.contract, store.lineage(sid, fifth['id']))), 1)
            self.assertEqual(journal['threads'][-1]['status'], 'resolved')
            self.assertEqual(journal['threads'][-1]['openedBranchId'], first['id'])
            self.assertEqual(journal['threads'][-1]['causeBranchId'], third['id'])
            self.assertEqual(journal['threads'][0]['status'], 'abandoned')
            sibling = advance(root, '留在原地等待', '你留在原地等候，没有继续问话。', [], 'sibling')
            self.assertEqual(read.journey(sid, sibling['id'])['threads'], root['branchState'][rt.STATE_KEY])
            self.assertEqual(read.journey(sid, first['id'])['threads'][-1]['status'], 'open')
            self.assertEqual(read.journey(sid, fifth['id'])['threads'], journal['threads'])
