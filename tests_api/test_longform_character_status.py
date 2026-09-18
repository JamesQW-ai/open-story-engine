"""Confirmed injuries/missing status on all official long novels, never absence inference."""
import copy
import hashlib
import unittest
from contextlib import closing

from open_story_engine import reader_actions as ra, reader_consequences as rc
from open_story_engine.character_presentation import known_status
from open_story_engine.reader_choices import choice_context
from open_story_engine.storage import SessionStore
from test_support.longform import longform_cases
from tests_api.test_longform_repair_fallback import LongformRepairFallbackTests


class LongformCharacterStatusTests(unittest.TestCase):
    def setUp(self):
        LongformRepairFallbackTests.setUp(self)
        _, snapshot = self.play._turn_snapshot(self.sid, self.parent)
        self.context = dict(package=snapshot.package, contract=snapshot.story_contract,
                            parent=snapshot.history[-1], lineage=snapshot.history, playerDirection='确认眼前人物的状态')
        self.target = next(c for c in snapshot.package['characters'] if c['id'] != self.cid)

    def result(self, status, body):
        plan = dict(decision='ready', requirements={'A1': {'mode': 'result', 'summary': self.context['playerDirection']}},
                    method='按现场证据确认', outcomes=[dict(characterId=self.target['id'], status=status,
                    permanence='temporary', requirementId='A1', cause='本回合明确确认', evidence=body)],
                    goalUpdates=[], introductions=dict(characters=[], items=[], locations=[]), stateChanges=[],
                    steps=[dict(id='S1', actorId=self.cid, action=self.context['playerDirection'],
                                requirementIds=['A1'], authority='player', causeStepId=None)])
        return dict(narrativeText=body, consequenceUpdate=plan, consequenceReview=rc.VERSION,
                    actionIntent={'input': self.context['playerDirection']},
                    reviewedNarrativeSha256=hashlib.sha256(body.encode()).hexdigest(),
                    authorityReview=dict(decision='allow', issues=[], stateChecks=[],
                        checks=[dict(stepId='S1', authorized=True, basis='player_input', quote=self.context['playerDirection'])]),
                    observedEvents=[dict(id='O1', paragraphId='P1', quote=body, actor=self.cid, mode='actual', summary=body,
                        changes=[dict(entityId=self.target['id'], attribute='outcome', value=status)], introduced=[])],
                    eventChecks=[dict(id='O1', verdict='supported', stepIds=['S1'], changeIds=[], introductionIds=[])])

    def commit(self, status):
        label = {'missing': '已确认失踪，去向不明', 'injured': '手臂受伤，伤口正在流血', 'alive': '已经回到眼前，确认仍然存活'}[status]
        result = self.result(status, self.target['name'] + label + '。')
        state = rc.commit_consequences(self.context, self.context['parent']['branchState'], result, status + '-branch')
        return {**self.context['parent'], **result, 'id': status + '-branch', 'branchState': state,
                'parentId': self.parent, 'sequence': 1, 'selectedDirectionId': 'status-action',
                'selectedDirection': {'id': 'status-action', 'isFreeText': True, 'statePatch': {}},
                'playerDirection': self.context['playerDirection']}

    def test_missing_and_injured_roundtrip_with_public_evidence_and_branch_isolation(self):
        for status in ('missing', 'injured'):
            with self.subTest(status=status):
                node = self.commit(status)
                with closing(SessionStore(str(self.read.database_path))) as store:
                    store.append_branch(self.sid, self.parent, node)
                with self.read.store() as reopened:
                    lineage = reopened.lineage(self.sid, node['id'])
                    root_lineage = reopened.lineage(self.sid, self.parent)
                self.assertEqual(known_status(lineage, self.target['id'])['code'], status)
                self.assertEqual(known_status(root_lineage, self.target['id'])['code'], 'unknown')
                self.assertEqual(known_status(lineage, self.target['id'])['evidence']['quote'], node['narrativeText'])
                self.assertIn(self.target['name'], rc.public_summary(node['consequenceUpdate'], self.context['package']))

    def test_missing_removes_old_location_and_cannot_offer_direct_interaction(self):
        node = self.commit('missing')
        self.assertNotIn(self.target['id'], node['branchState']['characterLocationIds'])
        context = choice_context(self.context['package'], self.context['contract'], self.context['lineage'], node)
        target = next(p for p in context['people'] if p['id'] == self.target['id'])
        self.assertFalse(target['available'])
        with self.assertRaisesRegex(ValueError, '失踪'):
            ra.require_available_actor(node['branchState'], self.target['id'])
        # Explicit recovery can be planned; commit still requires reviewed prose.
        ra.require_available_actor(node['branchState'], self.target['id'], [{'characterId': self.target['id'], 'status': 'alive'}])

    def test_missing_recovery_needs_new_evidence_and_does_not_restore_old_location(self):
        missing = self.commit('missing')
        self.context = {**self.context, 'parent': missing, 'lineage': [*self.context['lineage'], missing]}
        recovered = self.commit('alive')
        recovered['sequence'] = 2
        self.assertNotIn(self.target['id'], recovered['branchState']['characterLocationIds'])
        self.assertEqual(known_status([*self.context['lineage'], recovered], self.target['id'])['code'], 'alive')
        forged = copy.deepcopy(recovered)
        forged['narrativeText'] = '眼前无人出现。'
        self.assertEqual(known_status([*self.context['lineage'], forged], self.target['id'])['code'], 'unknown')

    def test_absence_and_unsupported_status_are_unknown(self):
        for status in ('missing', 'injured'):
            node = copy.deepcopy(self.context['parent'])
            node['branchState']['characterOutcomeStates'][self.target['id']] = dict(status=status, permanence='temporary')
            self.assertEqual(known_status([node], self.target['id'])['code'], 'unknown')

    def test_statuses_cannot_bypass_permanent_death_or_claim_permanent_missing(self):
        for status in ('missing', 'injured'):
            result = self.result(status, self.target['name'] + '的当前情况已经确认。')
            plan = result['consequenceUpdate']
            plan['outcomes'][0]['permanence'] = 'permanent'
            with self.assertRaisesRegex(ValueError, '不得推断'):
                rc.validate_plan(plan, {'A1': self.context['playerDirection']}, self.context)
            plan['outcomes'][0]['permanence'] = 'temporary'
            for old in ('dead', 'departed'):
                context = copy.deepcopy(self.context)
                context['parent']['branchState']['characterOutcomeStates'][self.target['id']] = dict(status=old, permanence='permanent')
                with self.assertRaisesRegex(ValueError, '不可撤销'):
                    rc.validate_plan(plan, {'A1': self.context['playerDirection']}, context)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformStatus_' + case['package_id'], (LongformCharacterStatusTests,), {'case': case})))
    return suite
