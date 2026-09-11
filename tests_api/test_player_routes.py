"""Playable route regression: branch-local progress, failures, replay and endings."""
import json
import unittest
from unittest.mock import patch

from open_story_engine import api_routes
from open_story_engine.api_journey import character_card
from open_story_engine.api_narrative import trim_optional_paragraphs, check_reader_repetition, complete_with_retry, apply_scene_repairs
from open_story_engine.api_narrative import PlayerNarrativePlanner, MAX_CHAPTER_CHARACTERS
from open_story_engine.cocreation import MockPlanner, plan_result, narrative_character_count
from open_story_engine.llm import Completion, LlmError
import test_play_api as play_fixture


class ScenePlanner(MockPlanner):
    interactive_reader = True
    published_directions = staticmethod(api_routes.directions)
    prepare_direction = staticmethod(api_routes.prepare_direction)

    def plan(self, context, selected, resolved_state, *args, **kwargs):
        name = api_routes.role_name(context['package'], resolved_state)
        step = api_routes.completed_steps(resolved_state)
        narrative = '你停下脚步，确认刚才行动的结果。' + api_routes.OUTCOMES[name][step-1]
        result = plan_result(context, selected, narrative, selected['title'],
                             api_routes.directions(context['package'], resolved_state), 'high')
        result['openThreads'] = [d['title'] for d in result['nextDirections']]
        return result, None


class PlayerRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = play_fixture.PlayApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.planner = patch('open_story_engine.api_play.MockPlanner', ScenePlanner)
        self.planner.start()
        self.addCleanup(self.planner.stop)
        # Rebuild the app so its test planner owns exactly the same scene rules.
        from open_story_engine.api import create_app
        from fastapi.testclient import TestClient
        self.client = TestClient(create_app(self.fixture.packages, self.fixture.database, play=True))
        self.addCleanup(self.client.close)

    def start(self, name):
        package = self.client.get('/api/v1/packages/rainy-waiting-room-source/0.1.16').json()
        role = next(c for c in package['characters'] if c['name'] == name)
        response = self.client.post('/api/v1/sessions', json={
            'package': {'package_id': 'rainy-waiting-room-source', 'version': '0.1.16'},
            'entry_point_id': self.fixture.entry['id'], 'source_character_id': role['id'], 'identity_opening': True,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['session']['id'], response.json()['branch']

    def journal(self, sid, node):
        response = self.client.get(f"/api/v1/sessions/{sid}/journey?branch_id={node['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_four_roles_reach_100_replay_is_idempotent_and_forks_keep_own_progress(self):
        for name in api_routes.SCENES:
            with self.subTest(role=name):
                sid, root = self.start(name)
                parent = root
                self.assertEqual(self.journal(sid, parent)['progress'], 0)
                for step, progress in enumerate([25, 50, 75, 100]):
                    self.assertGreaterEqual(len(parent['nextDirections']), 2)
                    payload = {'parent_branch_id': parent['id'], 'direction_id': parent['nextDirections'][step % 2]['id'],
                               'request_id': f'{name}-{step}'}
                    response = self.client.post(f'/api/v1/sessions/{sid}/branches', json=payload)
                    self.assertEqual(response.status_code, 200, response.text)
                    parent = response.json()['branch']
                    journal = self.journal(sid, parent)
                    self.assertEqual(journal['progress'], progress)
                    self.assertEqual(journal['status'], 'completed' if progress == 100 else 'active')
                self.assertEqual(parent['nextDirections'], [])
                self.assertEqual(parent['openThreads'], [])
                replay = self.client.post(f'/api/v1/sessions/{sid}/branches', json=payload).json()
                self.assertEqual(replay['branch']['id'], parent['id'])
                self.assertTrue(replay['deduplicated'])
                ended = self.client.post(f'/api/v1/sessions/{sid}/branches', json={
                    'parent_branch_id': parent['id'], 'text': '继续观察', 'request_id': 'after-ending'})
                self.assertEqual(ended.status_code, 409)
                fork = self.client.post(f'/api/v1/sessions/{sid}/branches', json={
                    'parent_branch_id': root['id'], 'direction_id': root['nextDirections'][1]['id'], 'request_id': name+'-fork'})
                self.assertEqual(fork.status_code, 200, fork.text)
                self.assertEqual(self.journal(sid, fork.json()['branch'])['progress'], 25)
                self.assertEqual(self.journal(sid, root)['progress'], 0)
                self.assertEqual(self.journal(sid, parent)['progress'], 100)

    def test_failed_free_action_does_not_advance_and_same_request_can_resume(self):
        sid, root = self.start('唐栖')
        payload = {'parent_branch_id': root['id'], 'text': '留在信号室，观察周围动静', 'request_id': 'resume-scene'}
        with patch.object(ScenePlanner, 'plan', side_effect=LlmError('temporary failure')):
            response = self.client.post(f'/api/v1/sessions/{sid}/branches', json=payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.journal(sid, root)['progress'], 0)
        self.assertEqual(len(self.client.get(f'/api/v1/sessions/{sid}/branches').json()['branches']), 1)
        response = self.client.post(f'/api/v1/sessions/{sid}/branches', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.journal(sid, response.json()['branch'])['progress'], 25)
        self.assertEqual(len(self.client.get(f'/api/v1/sessions/{sid}/branches').json()['branches']), 2)

    def test_short_complete_story_streams_and_saves_without_padding(self):
        from unittest.mock import Mock
        from open_story_engine.api_play import PlayService
        from open_story_engine.api_read import ReadService
        sid, root = self.start('唐栖')
        body = '你听见许川在门外回应，随即配合他推开卡住的门。铁门终于打开，你抱着文件袋跨出门槛。许川扶住你，陈砚站在旁边。姜序从岔口走来，说明水流已经减弱。你与许川、陈砚、姜序一起站在维修隧道，确认能够继续走。'
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                gateway = Mock(model='short-story')
                gateway.complete_text.side_effect = ([Completion('你'+('雨'*3500), '{}', [])] if oversized else []) + [Completion(body, '{}', [])]
                gateway.complete_json.side_effect = ([Completion('{"removable":[]}', '{}', [])] if oversized else []) + [
                    Completion(json.dumps({'final_location': '维修隧道', 'tang_safe': True, 'door_open': True,
                        'handoff_confirmed': False, 'evidence': body.split('。')[-2]+'。'}, ensure_ascii=False), '{}', []),
                    Completion('{"issues":[]}', '{}', []),
                ]
                service = PlayService(ReadService(self.fixture.packages, self.fixture.database), self.fixture.root)
                service._planner = PlayerNarrativePlanner(gateway)
                chunks = []
                result = service.continue_turn(sid, root['id'], text='敲门求助', request_id='short-turn-'+str(oversized), stream=chunks.append, stream_reset=lambda _: chunks.clear())
                self.assertEqual(result['branch']['narrativeText'], body)
                self.assertEqual(''.join(chunks), body)
                self.assertEqual(self.journal(sid, result['branch'])['progress'], 25)
                self.assertEqual(gateway.complete_text.call_count, 2 if oversized else 1)
                self.assertEqual(gateway.complete_json.call_count, 3 if oversized else 2)



class SceneEvidenceTests(unittest.TestCase):
    def test_local_repair_preserves_unaffected_text_and_rejects_wholesale_rewrite(self):
        paragraphs = ['甲' * 300, '乙' * 300, '丙' * 300, '丁' * 300]
        body = '\n\n'.join(paragraphs)
        fixed = apply_scene_repairs(body, [{'paragraphId': 'P2', 'text': '戊' * 300}])
        self.assertEqual(fixed.split('\n\n'), [paragraphs[0], '戊' * 300, *paragraphs[2:]])
        for edits in [[{'paragraphId': 'P9', 'text': '无效'}],
                      [{'paragraphId': 'P1', 'text': paragraphs[0]}],
                      [{'paragraphId': 'P'+str(i), 'text': '重写'} for i in range(1, 4)],
                      [{'paragraphId': 'P1', 'text': ''}]]:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                apply_scene_repairs(body, edits)

    def test_transient_call_retries_once_and_resets_only_its_preview(self):
        from unittest.mock import Mock
        gateway, reset, delta = Mock(), Mock(), Mock()
        error = LlmError('connection closed', 'transport_error')
        error.observations = [{'outcome': 'failed'}]
        gateway.complete_text.side_effect = [error, Completion('你走到灯下。', '{}', [{'outcome': 'completed'}])]
        result = complete_with_retry(gateway, 'complete_text', [], delta, reset)
        self.assertEqual(result.content, '你走到灯下。')
        self.assertEqual([o['outcome'] for o in result.observations], ['failed', 'completed'])
        reset.assert_called_once_with('connection_retry')
        gateway.complete_text.reset_mock()
        gateway.complete_text.side_effect = LlmError('still unavailable', 'transport_error')
        with self.assertRaises(LlmError):
            complete_with_retry(gateway, 'complete_text', [])
        self.assertEqual(gateway.complete_text.call_count, 2)

    def test_character_cards_require_present_scene_not_memory_or_another_route(self):
        character = {'id': 'tang', 'name': '唐栖', 'menuDescription': '调查者'}
        nodes = [{'sequence': 0, 'narrativeText': '你想起唐栖下午留下的语音。“唐栖究竟在哪？”你问。'}]
        self.assertIsNone(character_card(nodes, character, '许川'))
        absent = [{'sequence': 0, 'narrativeText': '唐栖没有出现。录音笔里传来唐栖的声音。'}]
        self.assertIsNone(character_card(absent, character, '许川'))
        appeared = {'sequence': 1, 'narrativeText': '门开了，你看见唐栖。唐栖扶着门框，抬眼望向你。'}
        self.assertEqual(character_card(nodes+[appeared], character, '许川')['first_page'], 2)
        self.assertIsNone(character_card(nodes, character, '许川'))
        self.assertIsNone(character_card([{'sequence': 0, 'narrativeText': ''}], character, '唐栖'))

    def test_unregistered_witness_dates_and_recording_confusion_are_not_saved_as_facts(self):
        package = {'sourceAnalysis': {'sha256': api_routes.RAINY_SOURCE},
                   'characters': [{'id': 'tang', 'name': '唐栖'}, {'id': 'xu', 'name': '许川'}]}
        state = {'storyScope': 'source', 'playerCharacterId': 'tang', 'derivedEvents': []}
        for text in ['你打开门，周师傅说他曾签收材料。', '你检查门锁，档案写着7月14日。', '你在门边按下录音笔，播放求助语音。', '姜序拿起撬棍走到门外。']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                api_routes.check_scene_result(package, state, text)
        api_routes.check_scene_result(package, state, '你隔着门说清门锁情况，许川在门外回应。')
        api_routes.check_scene_result(package, state, '你在门边收好录音笔，用手机播放求助语音。')
        api_routes.check_scene_result(package, state, '门开着，录音笔贴着你，最初的求助语音还留在手机里。')
        evidence_state = {**state, 'derivedEvents': [{'id': api_routes.PREFIX+str(i)} for i in (1, 2)]}
        for text in ['姜序指出签名的末笔有回勾。', '你发现验收单两组数字对不上。', '姜序说档案编号跳了一页。']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                api_routes.check_scene_result(package, evidence_state, text)
        self.assertIsNone(api_routes.role_name({**package, 'sourceAnalysis': {'sha256': 'another-novel'}}, state))
        self.assertIsNone(api_routes.role_name(package, {**state, 'sourceProgress': 'chapter_014'}))

    def test_return_to_hall_depends_on_role_and_ending_preserves_known_history(self):
        package = {'sourceAnalysis': {'sha256': api_routes.RAINY_SOURCE},
                   'characters': [{'id': 'tang', 'name': '唐栖'}, {'id': 'xu', 'name': '许川'}]}
        state = {'storyScope': 'source', 'playerCharacterId': 'xu',
                 'derivedEvents': [{'id': api_routes.PREFIX+str(i)} for i in (1, 2)]}
        api_routes.check_scene_result(package, state, '你沿来路走，再回候车厅。')
        with self.assertRaises(ValueError):
            api_routes.check_scene_result(package, {**state, 'playerCharacterId': 'tang'}, '你核对签名，再回候车厅。')
        state['derivedEvents'].append({'id': api_routes.PREFIX+'3'})
        for claim in ['许川还没听过录音。', '你下午发的那条语音。', '姜序把日志放进抽屉。']:
            with self.subTest(claim=claim), self.assertRaises(ValueError):
                api_routes.check_scene_result(package, state, '列车停站，日志和事故上报已经交代。'+claim)
        api_routes.check_scene_result(package, state, '列车停站，姜序保管日志，司机负责事故上报。')

    def test_length_edit_protects_first_and_final_outcome_paragraphs(self):
        paragraphs = ['你来到门边。' + '甲' * 498, '乙' * 500, '丙' * 500, '丁' * 500, '戊' * 500, '众人获救，留下真实记录。' + '己' * 498]
        edited = trim_optional_paragraphs('\n\n'.join(paragraphs), [{'unexpected': 'P2'}, 'P1', 'P2', 'P3', 'P5', 'P6'], maximum=2500)
        self.assertTrue(2000 <= narrative_character_count(edited) <= 2500)
        self.assertTrue(edited.startswith(paragraphs[0]))
        self.assertTrue(edited.endswith(paragraphs[-1]))
        self.assertIn(paragraphs[-2], edited)
        short = '你听见回应，停下脚步。'
        self.assertEqual(trim_optional_paragraphs(short, []), short)
        self.assertEqual(MAX_CHAPTER_CHARACTERS, 3500)

    def test_short_dialogue_loop_is_rejected_without_rejecting_repeated_names(self):
        scene = '\n\n'.join('你向姜序询问第' + str(i) + '处记录，他指着纸面讲明原因。' for i in range(9))
        check_reader_repetition(scene)
        with self.assertRaises(ValueError):
            check_reader_repetition(scene + '\n\n' + scene)

    def test_reviewed_story_ending_must_match_pending_milestone(self):
        package = {'sourceAnalysis': {'sha256': api_routes.RAINY_SOURCE},
                   'characters': [{'id': 'tang', 'name': '唐栖'}]}
        state = {'storyScope': 'source', 'playerCharacterId': 'tang', 'derivedEvents': []}
        quote = '铁门终于打开，你抱着文件袋跨出门槛，站在维修隧道里。'
        ending = {'final_location': '维修隧道', 'door_open': True, 'tang_safe': True, 'evidence': quote}
        api_routes.check_reviewed_ending(package, state, quote, ending)
        for bad in [{**ending, 'door_open': False}, {**ending, 'final_location': '候车厅'},
                    {**ending, 'evidence': '这是一条没有出现在正文中的假引文。'}, None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                api_routes.check_reviewed_ending(package, state, quote, bad)
        later = {**state, 'derivedEvents': [{'id': api_routes.PREFIX+'1'}]}
        later_quote = '你终于走进候车厅，把文件袋抱在怀里，靠着长椅坐下来。'
        # No demand to re-open or re-describe a door left behind last chapter.
        api_routes.check_reviewed_ending(package, later, later_quote,
            {'final_location': '候车厅', 'door_open': None, 'tang_safe': None, 'evidence': later_quote})


if __name__ == '__main__':
    unittest.main()
