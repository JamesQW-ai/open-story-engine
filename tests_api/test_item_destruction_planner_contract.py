"""Offline contract replay for permanent-destruction planner boundaries."""
import copy
import json
import unittest
from pathlib import Path

from open_story_engine import item_lifecycle as items
from open_story_engine import reader_consequences as rc
from open_story_engine.api_narrative import player_package
from open_story_engine.api_reader_quality import action_requirements
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import create_contract, entry_node
from open_story_engine.reader_scene_plan import validate_scene_plan
from open_story_engine.reader_scene_review import public_scene_evidence
from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.llm import Completion


ROOT = Path(__file__).resolve().parents[1]
LU = 'character_ae4cb42b9b49'
ITEM = 'item_open_letter'


class ItemDestructionPlannerContractTests(unittest.TestCase):
    def setUp(self):
        package = player_package(load_runtime_story_package(
            ROOT / 'content/packages/taixu-relics-part1/0.1.3/package.json', lazy=True), LU)
        contract = create_contract(package, 'fixture', {
            'kind': 'source_character', 'sourceCharacterId': LU, 'entryPointId': 'entry_lu_gate',
        })
        root = entry_node(package, contract)
        self.context = dict(package=package, contract=contract, parent=root, lineage=[root])
        self.state = copy.deepcopy(root['branchState'])
        self.action = '我永久损毁道具：引荐文书。'
        evidence = public_scene_evidence(self.context)
        self.source_id = next(key for key in evidence if key.startswith('history-') and key.endswith('-P1'))
        self.evidence = evidence

    def abstract_plan(self):
        return {
            'decision': 'ready', 'readingIntent': 'brief',
            'requirements': {'A1': {'mode': 'result', 'summary': '永久损毁引荐文书，具体表现待正文确认'}},
            'method': '依据当前持有与可损毁性，登记引荐文书的永久损毁结果；具体表现待正文确认。',
            'outcomes': [], 'goalUpdates': [], 'threadUpdates': [],
            'steps': [{'id': 'S1', 'actorId': LU, 'action': '永久损毁当前持有的引荐文书，具体表现待确认',
                       'requirementIds': ['A1'], 'authority': 'player', 'causeStepId': None,
                       'usedItemIds': [ITEM]}],
            'introductions': {'characters': [], 'items': [], 'locations': []},
            'stateChanges': [{'id': 'C1', 'entityId': ITEM, 'attribute': items.ATTRIBUTE,
                              'before': None, 'value': True, 'stepId': 'S1',
                              'reason': '玩家明确要求永久损毁当前持有的引荐文书'}],
            'scenePlan': {
                'start': '承接当前山门外的雨声与手中文书。',
                'beats': [{'purpose': '保留抽象的永久损毁结果，具体表现留待正文确认。', 'stepIds': ['S1']}],
                'outcome': '引荐文书的永久损毁结果待正文完成并核验。',
                'stop': '将下一步决定留给玩家。',
                'targetCjk': [80, 180],
                'lengthReason': '单一不可逆结果，短段落足够。',
                'narrativeOptions': [{'text': '撕扯或焚烧等具体表现，须等正文确认',
                                      'status': 'pending', 'requiresConfirmation': True}],
                'knowledge': [{
                    'speakerId': LU, 'statement': '引荐文书当前由自己持有，尚未永久损毁。',
                    'status': 'reported',
                    'sources': [{'id': self.source_id, 'quote': self.evidence[self.source_id]}],
                }],
                'observationLimits': ['具体损毁方式与守门弟子是否看见仍未知。'],
            },
        }

    def validate(self, plan):
        requirements = action_requirements(self.action)
        checked = rc.validate_plan(plan, requirements, self.context)
        validate_scene_plan(checked, self.evidence, {LU})
        return checked

    def test_explicit_result_stays_abstract_and_uncommitted(self):
        before = copy.deepcopy(self.state)
        checked = self.validate(self.abstract_plan())
        self.assertTrue(any(c['attribute'] == items.ATTRIBUTE and c['value'] is True
                            for c in checked['stateChanges']))
        self.assertEqual(self.state, before)
        self.assertFalse(items.destroyed(self.state, ITEM))
        self.assertEqual(self.state['itemOwnerCharacterIds'][ITEM], LU)

    def test_unspecified_physical_action_is_rejected(self):
        plan = self.abstract_plan()
        plan['steps'][0]['action'] = '把引荐文书撕碎揉烂'
        with self.assertRaisesRegex(ValueError, '具体.*动作'):
            self.validate(plan)

    def test_pending_candidate_must_not_leak_into_canonical_fields(self):
        plan = self.abstract_plan()
        plan['narrativeOptions'] = [{'text': '撕毁或揉散，待正文确认',
                                     'status': 'pending', 'requiresConfirmation': True}]
        plan['method'] = '先取出文书，再撕毁并揉散。'
        with self.assertRaisesRegex(ValueError, '具体.*动作'):
            self.validate(plan)

    def test_unsupported_witness_is_rejected(self):
        plan = self.abstract_plan()
        plan['scenePlan']['outcome'] = '守门弟子目睹陆照临永久损毁引荐文书。'
        with self.assertRaisesRegex(ValueError, '目睹.*依据'):
            self.validate(plan)

        plan = self.abstract_plan()
        plan['requirements']['A1']['summary'] = '永久损毁引荐文书，并由守门弟子确认。'
        with self.assertRaisesRegex(ValueError, '目睹或确认.*依据'):
            self.validate(plan)

    def test_explicitly_unknown_witness_limit_is_allowed(self):
        plan = self.abstract_plan()
        plan['scenePlan']['observationLimits'][0] = '没有依据表明守门弟子或伤者看清、听见或确认损毁动作。'
        self.validate(plan)

    def test_holding_quote_cannot_prove_destruction(self):
        plan = self.abstract_plan()
        plan['scenePlan']['knowledge'][0]['statement'] = '引荐文书已经永久损毁。'
        with self.assertRaisesRegex(ValueError, '持有.*不能证明'):
            self.validate(plan)

    def test_missing_irreversible_result_is_rejected_before_authority(self):
        plan = self.abstract_plan()
        plan['stateChanges'] = []
        with self.assertRaisesRegex(ValueError, '没有登记不可逆'):
            self.validate(plan)

    def test_valid_canonical_plan_still_stops_at_authority_before_any_write(self):
        plan = self.abstract_plan()
        evidence = self.evidence
        review = {
            'decision': 'allow', 'issues': [],
            'checks': [{'stepId': 'S1', 'authorized': True, 'basis': 'player_input',
                        'quote': self.action, 'reason': '只授权抽象的永久损毁结果'}],
            'stateChecks': [{'changeId': 'C1', 'authorized': True, 'reason': '不可逆结果与玩家请求一致'}],
            'premiseChecks': [{
                'id': 'K1', 'kind': 'existing', 'verdict': 'supported',
                'sources': [{'id': self.source_id, 'quote': evidence[self.source_id]}],
                'stepIds': [], 'missingEvidence': [],
                'reason': '性质：道具当前由玩家持有，未证明已损毁。依据：公开引文确认持有。缺证内容：具体损毁方式与NPC反应。',
            }, {
                'id': 'O1', 'kind': 'restriction', 'verdict': 'supported', 'sources': [], 'stepIds': [],
                'missingEvidence': [], 'reason': '具体损毁方式与现场反应保持未知。',
            }],
        }
        before = copy.deepcopy(self.state)
        gateway = type('Gateway', (), {})()
        gateway.model = 'fixture'
        gateway.complete_json = lambda *args, **kwargs: Completion(json.dumps(review, ensure_ascii=False), '{}', [])
        ra_result = PlayerNarrativePlanner(gateway)._check_action_authority(
            self.context, self.action, plan, [], [])
        self.assertEqual(ra_result['decision'], 'allow')
        self.assertEqual(self.state, before)
        self.assertFalse(items.destroyed(self.state, ITEM))


if __name__ == '__main__':
    unittest.main()
