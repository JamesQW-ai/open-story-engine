"""Offline replay for permanent item authority and lifecycle boundaries."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from open_story_engine import item_lifecycle as items, reader_actions as ra
from open_story_engine.api_narrative import PlayerNarrativePlanner, player_package
from open_story_engine.cocreation import create_contract, entry_node
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import Completion, LlmError
from open_story_engine.reader_scene_review import public_scene_evidence


ROOT = Path(__file__).resolve().parents[1]
LU = 'character_ae4cb42b9b49'
ITEM = 'item_open_letter'


class ItemDestructionAuthorityTests(unittest.TestCase):
    def setUp(self):
        package = player_package(load_runtime_story_package(
            ROOT / 'content/packages/taixu-relics-part1/0.1.3/package.json', lazy=True), LU)
        contract = create_contract(package, 'fixture', {
            'kind': 'source_character', 'sourceCharacterId': LU, 'entryPointId': 'entry_lu_gate',
        })
        root = entry_node(package, contract)
        self.context = dict(package=package, contract=contract, parent=root, lineage=[root])
        self.state = root['branchState']
        self.item = next(item for item in package['items'] if item['id'] == ITEM)
        self.action = '我永久损毁道具：引荐文书。'

    def destruction_plan(self):
        return {
            'decision': 'ready',
            'requirements': {'A1': {'mode': 'result', 'summary': self.action}},
            'method': '玩家撕碎当前持有的引荐文书，使其不可复原。',
            'outcomes': [], 'goalUpdates': [], 'threadUpdates': [],
            'steps': [{'id': 'S1', 'actorId': LU, 'action': '撕碎手中的引荐文书',
                       'requirementIds': ['A1'], 'authority': 'player', 'causeStepId': None,
                       'usedItemIds': [ITEM]}],
            'introductions': {'characters': [], 'items': [], 'locations': []},
            'stateChanges': [{'id': 'C1', 'entityId': ITEM, 'attribute': items.ATTRIBUTE,
                              'before': None, 'value': True, 'stepId': 'S1',
                              'reason': '玩家明确要求将当前持有的文书永久损毁'}],
        }

    def authority_contract(self):
        plan = self.destruction_plan()
        evidence = public_scene_evidence(self.context)
        source_id = next(key for key in evidence if key.startswith('history-') and key.endswith('-P1'))
        scene_plan = {
            'knowledge': [{
                'speakerId': LU,
                'statement': '引荐文书当前由玩家持有，尚未永久损毁。',
                'status': 'reported',
                'sources': [{'id': source_id, 'quote': evidence[source_id]}],
            }],
            'observationLimits': [],
        }
        plan['scenePlan'] = scene_plan
        review = {
            'decision': 'allow', 'issues': [],
            'checks': [{'stepId': 'S1', 'authorized': True, 'basis': 'player_input',
                        'quote': self.action, 'reason': '玩家明确授权永久损毁'}],
            'stateChecks': [{'changeId': 'C1', 'authorized': True,
                             'reason': '状态变化与玩家输入一致'}],
            'premiseChecks': [{
                'id': 'K1', 'kind': 'existing', 'verdict': 'supported',
                'sources': [{'id': source_id, 'quote': evidence[source_id]}],
                'stepIds': [], 'missingEvidence': [],
                'reason': '性质：登记道具当前由玩家持有且尚未损毁。依据：前文逐字确认手中有引荐文书。缺证内容：无。',
            }],
        }
        return plan, review

    def test_legal_held_item_has_a_complete_k1_and_passes_authority(self):
        plan, review = self.authority_contract()
        ra.validate_plan(plan, self.context)
        gateway = Mock(model='fixture')
        gateway.complete_json.return_value = Completion(json.dumps(review, ensure_ascii=False), '{}', [])
        result = PlayerNarrativePlanner(gateway)._check_action_authority(
            self.context, self.action, plan, [], [])
        self.assertEqual(result['premiseChecks'][0]['missingEvidence'], [])
        self.assertEqual(gateway.complete_json.call_count, 1)

    def test_missing_k1_field_is_explicit_internal_failure_without_state_write(self):
        plan, review = self.authority_contract()
        review['premiseChecks'][0].pop('missingEvidence')
        gateway = Mock(model='fixture')
        gateway.complete_json.side_effect = [
            Completion(json.dumps(review, ensure_ascii=False), '{}', []),
            Completion(json.dumps(review, ensure_ascii=False), '{}', []),
        ]
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(LlmError, 'K1.*missingEvidence'):
            PlayerNarrativePlanner(gateway)._check_action_authority(
                self.context, self.action, plan, [], [])
        self.assertEqual(self.state, before)
        self.assertEqual(gateway.complete_json.call_count, 2)

    def test_lifecycle_requires_current_player_holding_and_declared_capability(self):
        plan = self.destruction_plan()
        ra.validate_plan(plan, self.context)

        missing_owner = copy.deepcopy(self.state)
        missing_owner['itemOwnerCharacterIds'].pop(ITEM)
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': missing_owner}}
        with self.assertRaisesRegex(ValueError, '当前持有'):
            ra.validate_plan(copy.deepcopy(plan), context)

        non_destroyable = copy.deepcopy(self.context['package'])
        next(item for item in non_destroyable['items'] if item['id'] == ITEM)['destructible'] = False
        context = {**self.context, 'package': non_destroyable}
        with self.assertRaisesRegex(ValueError, '可损毁性'):
            ra.validate_plan(copy.deepcopy(plan), context)

    def test_already_destroyed_item_cannot_be_marked_again(self):
        state = copy.deepcopy(self.state)
        state['readerEntityStates'] = {ITEM: {items.ATTRIBUTE: True}}
        context = {**self.context, 'parent': {**self.context['parent'], 'branchState': state}}
        plan = self.destruction_plan()
        plan['stateChanges'][0]['before'] = True
        with self.assertRaisesRegex(ValueError, '再次损毁'):
            ra.validate_plan(plan, context)


if __name__ == '__main__':
    unittest.main()
