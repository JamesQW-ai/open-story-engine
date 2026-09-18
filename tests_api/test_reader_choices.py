import copy
import json
import unittest
from unittest.mock import Mock, patch

from open_story_engine.api_reader_quality import scene_pacing
from open_story_engine.api_turn_drafts import visible_choices
from open_story_engine.cocreation import MockPlanner, scripted_followup_directions
from open_story_engine.llm import LlmError
from open_story_engine.prompts import render_prompt
from open_story_engine.reader_choices import choice_context, validate_choices, generate_choices


class ReaderChoiceTests(unittest.TestCase):
    def setUp(self):
        self.context = {'paragraphs': {'P1': '你捏住断牌，陆照临等着你的答复。'},
                        'people': [{'id': 'lu', 'name': '陆照临', 'available': True}]}
        self.package = {'characters': [{'id': 'lu', 'name': '陆照临'}, {'id': 'hidden', 'name': '未见之人'}]}
        self.option = {'title': '询问接下来的安排', 'action': '我先询问陆照临接下来的安排，等他明确答复后再决定是否出发。',
                       'paragraphIds': ['P1'], 'interactWith': ['lu']}

    def test_dynamic_count_deduplication_and_no_authority_in_suggestions(self):
        choices = validate_choices({'choices': [self.option, copy.deepcopy(self.option)]}, self.context, self.package)
        self.assertEqual(len(choices), 1)
        self.assertNotIn('statePatch', choices[0])
        parent = {'kind': 'generated', 'readerChoices': choices, 'nextDirections': []}
        with patch('open_story_engine.reader_choices.choice_context', return_value=self.context):
            menu = visible_choices(parent, self.package, {}, [parent])
        self.assertEqual(menu[0]['payload'], {'text': self.option['action']})
        self.assertEqual(visible_choices({'kind': 'generated', 'nextDirections': []}, {}), [])
        opening = {'kind': 'source_entry', 'openingActions': [{'title': str(i), 'summary': '行动'} for i in range(4)]}
        self.assertEqual(len(visible_choices(opening, {})), 4)

    def test_unknown_people_dead_interactions_and_fake_evidence_are_rejected(self):
        for invalid in [dict(self.option, paragraphIds=['P99']), dict(self.option, interactWith=['hidden']),
                        dict(self.option, action='我向未见之人询问现在应该怎么做。')]:
            with self.assertRaises(ValueError):
                validate_choices({'choices': [invalid]}, self.context, self.package)
        self.context['people'][0]['available'] = False
        with self.assertRaises(ValueError):
            validate_choices({'choices': [self.option]}, self.context, self.package)

    def test_named_person_cannot_be_omitted_but_aftermath_can_be_declared(self):
        self.context['people'][0]['available'] = False
        with self.assertRaises(ValueError):
            validate_choices({'choices': [dict(self.option, interactWith=[])]}, self.context, self.package)
        aftermath = dict(self.option, title='整理留下的线索', action='我整理陆照临留下的线索，暂时不离开原地。',
                         interactWith=[], mentionOnly=['lu'])
        result = validate_choices({'choices': [aftermath]}, self.context, self.package)
        self.assertEqual(result[0]['dependencies']['mentionOnly'], ['lu'])
        from open_story_engine.reader_choices import restored_choices
        self.assertEqual(restored_choices(result, self.context, self.package), result)
        for invalid in (dict(aftermath, interactWith=['lu']), dict(aftermath, mentionOnly=['unknown']),
                        dict(aftermath, mentionOnly='lu')):
            with self.subTest(option=invalid), self.assertRaises(ValueError):
                validate_choices({'choices': [invalid]}, self.context, self.package)

    def test_restore_rechecks_dependency_metadata_and_context_without_mutation(self):
        from open_story_engine.reader_choices import restored_choices
        stored = validate_choices({'choices': [self.option]}, self.context, self.package)
        original = copy.deepcopy(stored)
        self.assertEqual(restored_choices(stored, self.context, self.package), stored)
        dead = copy.deepcopy(self.context)
        dead['people'][0]['available'] = False
        self.assertEqual(restored_choices(stored, dead, self.package), [])
        legacy = [{k: v for k, v in stored[0].items() if k != 'dependencies'}]
        self.assertEqual(restored_choices(legacy, self.context, self.package), [])
        changed = copy.deepcopy(stored)
        changed[0]['summary'] = '我立即要求陆照临带我离开，再交出随身物品。'
        self.assertEqual(restored_choices(changed, self.context, self.package), [])
        unknown_version = copy.deepcopy(stored)
        unknown_version[0]['dependencies']['version'] = 'obsolete'
        self.assertEqual(restored_choices(unknown_version, self.context, self.package), [])
        for malformed in (None, {}, [None], [{'dependencies': []}]):
            self.assertEqual(restored_choices(malformed, self.context, self.package), [])
        self.assertEqual(stored, original)

    def test_changed_item_or_goal_invalidates_old_suggestion(self):
        from open_story_engine.reader_choices import restored_choices
        self.context.update(items=[dict(id='token', state={'完整性': '完整'})],
                            goals=[dict(id='goal', status='active')])
        stored = validate_choices({'choices': [self.option]}, self.context, self.package)
        for field, value in (('items', [dict(id='token', state={'完整性': '断裂'})]),
                             ('goals', [dict(id='goal', status='abandoned')]),
                             ('playerLocationId', 'another-place'),
                             ('paragraphs', {'P1': '你已经转身离开。'})):
            context = {**self.context, field: value}
            self.assertEqual(restored_choices(stored, context, self.package), [])

    def test_unverifiable_menu_does_not_fall_back_to_old_directions(self):
        parent = {'kind': 'generated', 'readerChoices': [dict(id='old', title='询问旧安排', summary='向他询问')],
                  'nextDirections': [dict(id='fallback', title='恢复旧安排', summary='继续同行')]}
        with patch('open_story_engine.reader_choices.choice_context', return_value=self.context):
            self.assertEqual(visible_choices(parent, self.package, {}, [parent]), [])

    def test_fallback_filters_derived_people_and_ended_goal_directions(self):
        from open_story_engine.reader_consequences import filter_directions, goal_directions
        state = {'freeTextProgress': 1, 'derivedCharacters': [dict(id='visitor', name='远行客')],
                 'characterOutcomeStates': {'visitor': dict(status='departed', permanence='permanent')},
                 'goalLedger': [dict(id='old', title='同行', status='abandoned', source='player_branch'),
                                dict(id='new', title='处理远行客留下的信', status='active', source='player_branch')]}
        valid = goal_directions(state)[0]
        stale = dict(id='goal-direction-old-2', title='继续同行', summary='完成原目标', statePatch={})
        unavailable = dict(id='source', title='追问远行客', summary='听取答复', statePatch={})
        self.assertEqual(filter_directions([stale, unavailable, valid], {}, state), [valid])
        parent = dict(kind='generated', branchState=state, nextDirections=[stale, unavailable, valid])
        self.assertEqual([c['id'] for c in visible_choices(parent, {})], [valid['id']])

        context = {'package': {}, 'parent': {}}
        custom = {'id': 'custom', 'title': '自定行动', 'summary': '继续核对眼前线索', 'isFreeText': True}
        self.assertEqual(len(scripted_followup_directions(context, custom, {'freeTextProgress': 1})), 1)
        self.assertEqual(len(scripted_followup_directions(context, custom, {'freeTextProgress': 2})), 1)
        self.assertEqual(len(scripted_followup_directions(context, custom, {'freeTextProgress': 3})), 1)
        mock_context = {'package': {'id': 'taixu-relics-part1'}, 'parent': {}}
        self.assertEqual(MockPlanner()._next(mock_context, custom, {'freeTextProgress': 3}), [])

    def test_menu_failure_keeps_valid_prose_available(self):
        package = {'id': 'book', 'characters': [], 'story': {}}
        contract = {'persona': {'name': '旅人'}}
        node = {'branchState': {}, 'narrativeText': '风停了。'}
        history = [dict(node, sequence=0)]
        gateway = Mock(model='fixture')
        gateway.complete_json.side_effect = LlmError('连接失败', 'transport_error')
        choices, audit = generate_choices(gateway, package, contract, history, node)
        self.assertIsNone(choices)
        self.assertIn('error', audit)
        self.assertEqual(node['narrativeText'], '风停了。')
        gateway.complete_json.assert_called_once()

    def test_direction_context_uses_public_item_state_and_goal_history(self):
        from tests_api.test_reader_consequences import ConsequenceTests, GU, LU
        fixture = ConsequenceTests()
        fixture.setUp()
        root = dict(fixture.root, sequence=0)
        state = copy.deepcopy(root['branchState'])
        token = 'item_open_gu_token'
        state['readerEntityStates'] = {token: {'完整性': '断成两截'}}
        state['characterOutcomeStates'][LU] = dict(status='departed', permanence='permanent')
        state['goalLedger'] = [dict(id='old', title='继续试炼', status='abandoned', dependencies=[LU]),
                               dict(id='new', title='寻找落脚处', status='active', dependencies=[])]
        state['derivedItems'] = [dict(id='item_secret', name='密钥', summary='尚未公开的物品')]
        node = dict(root, branchState=state, narrativeText='你将木牌折成两截，决定放下试炼，先寻找落脚处。')
        before = copy.deepcopy(node)
        context = choice_context(fixture.package, fixture.contract, [root], node)
        items = {item['id']: item for item in context['items']}
        self.assertEqual(items[token]['state'], {'完整性': '断成两截'})
        self.assertEqual(items[token]['ownerCharacterId'], GU)
        self.assertNotIn('item_secret', items)
        self.assertNotIn('item_22e503a8a555', items)  # Future source prop, not public inventory.
        lu = next(p for p in context['people'] if p['id'] == LU)
        self.assertEqual((lu['status'], lu['permanence'], lu['available']), ('departed', 'permanent', False))
        self.assertEqual(context['goals'], state['goalLedger'])
        # Building one branch's menu must not mutate any branch or its ledger.
        context['items'][0]['state']['changed'] = True
        context['goals'][0]['status'] = 'active'
        self.assertEqual(node, before)
        sibling = choice_context(fixture.package, fixture.contract, [root], root)
        self.assertEqual(next(i for i in sibling['items'] if i['id'] == token)['state'], {})

    def test_item_knowledge_requires_inventory_or_committed_public_evidence(self):
        package = {'characters': [], 'items': [], 'story': {}}
        contract = {'persona': {'name': '旅人', 'sourceCharacterId': 'player'}}
        root = {'sequence': 0, 'branchState': {}, 'narrativeText': '你留在原地。'}
        body = '旅人折出一个纸标，放在地上。'
        item = dict(id='item_marker', name='纸标', summary='折成的标记', evidence=body)
        node = {'branchState': {'derivedItems': [item]}, 'narrativeText': body,
                'consequenceUpdate': {'introductions': {'items': [item]}}}
        context = choice_context(package, contract, [root], node)
        self.assertEqual(context['items'], [dict(id='item_marker', name='纸标', state={},
                                                ownerCharacterId='unknown', locationId='unknown')])
        node['consequenceUpdate']['introductions']['items'][0]['evidence'] = '不存在的正文'
        self.assertEqual(choice_context(package, contract, [root], node)['items'], [])
        node['consequenceUpdate'] = {'stateChanges': [dict(entityId='item_marker', evidence=body)]}
        self.assertEqual(len(choice_context(package, contract, [root], node)['items']), 1)

    def test_scene_budget_follows_authorized_work_without_forcing_extra_actions(self):
        before = {'playerLocationId': 'yard'}
        short = scene_pacing({'steps': [{}], 'readingIntent': 'brief'}, before, before)
        plan = {'targetCjk': [350, 650], 'lengthReason': '对话涉及两种不同判断，需要充分回应'}
        regular = scene_pacing({'scenePlan': plan, 'steps': [{}]}, before, before)
        complex_scene = scene_pacing({'scenePlan': plan, 'steps': [{}] * 5}, before, {'playerLocationId': 'gate'})
        self.assertEqual(regular, complex_scene)
        self.assertEqual([x['level'] for x in [short, regular, complex_scene]], ['brief', 'dynamic', 'dynamic'])
        self.assertTrue(all(not x['hardMinimum'] for x in [short, regular, complex_scene]))
        # Exercise the new strict field contract, not a snapshot rewritten to pass.
        from open_story_engine.prompts.registry import _catalog
        fields = _catalog()._templates['reader.narrative'][1]
        prompt = render_prompt('reader.narrative', **{k: json.dumps(regular) if k == 'pacing_json' else '' for k in fields})
        self.assertIn('"targetCjk": [350, 650]', prompt)
        self.assertIn('scenePlan', prompt)
