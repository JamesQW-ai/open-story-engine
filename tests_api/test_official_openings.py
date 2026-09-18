"""HTTP start must preserve the same official snapshots as CLI and lazy modules."""
import os
from contextlib import closing
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from open_story_engine.api import create_app
from open_story_engine.api_narrative import PlayerNarrativePlanner, player_package
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import create_contract, entry_node
from open_story_engine.llm import Completion, LlmError

ROOT = Path(__file__).resolve().parents[1]


class OfficialOpeningApiTests(unittest.TestCase):
    def test_rejected_opening_is_rewritten_and_cannot_silently_pass(self):
        package = load_runtime_story_package(ROOT / 'content/packages/taixu-relics-part1/0.1.2/package.json', lazy=True)
        package = player_package(package, 'character_ae4cb42b9b49')
        root = entry_node(package, create_contract(package, 'test'))
        gateway = Mock()
        gateway.complete_text.side_effect = [Completion(root['narrativeText'], '', []), Completion(root['narrativeText'], '', [])]
        gateway.complete_json.side_effect = [Completion(json.dumps({'passed': False, 'issues': ['道具位置不符']}), '', []),
                                            Completion(json.dumps({'passed': True, 'issues': []}), '', [])]
        planner = PlayerNarrativePlanner(gateway)
        reset = Mock()
        self.assertEqual(planner.opening(package, root, '陆照临', stream_reset=reset), root['narrativeText'])
        reset.assert_called_once_with('opening_revision')
        gateway.complete_text.side_effect = None
        gateway.complete_text.return_value = Completion(root['narrativeText'], '', [])
        gateway.complete_json.side_effect = None
        gateway.complete_json.return_value = Completion('{"passed":false,"issues":["人物越权"]}', '', [])
        with self.assertRaises(LlmError):
            planner.opening(package, root, '陆照临')

    def test_authored_opening_stream_skips_model_and_duplicate_reuses_root(self):
        from open_story_engine.api_play import PlayService
        from open_story_engine.storage import SessionStore
        def runtime(service):
            service._planner = PlayerNarrativePlanner(Mock())
        opening = Mock(side_effect=AssertionError('authored identity opening must not call the model'))
        with tempfile.TemporaryDirectory() as directory, patch.object(PlayService, '_build_runtime', runtime), \
                patch.object(PlayerNarrativePlanner, 'opening', opening):
            database = Path(directory) / 'sessions.sqlite'
            with TestClient(create_app(ROOT / 'content/packages', database, play=True)) as client:
                payload = dict(package={'package_id': 'taixu-relics-part1', 'version': '0.1.2'},
                               entry_point_id='entry_lu_gate', source_character_id='character_ae4cb42b9b49',
                               identity_opening=True, request_id='stable-opening-request')
                good = client.post('/api/v1/sessions/stream', json=payload)
                self.assertIn('event: done', good.text)
                repeated = client.post('/api/v1/sessions/stream', json=payload)
                self.assertIn('event: done', repeated.text)
                self.assertNotIn('event: delta', repeated.text)
                opening.assert_not_called()
                with closing(SessionStore(str(database))) as store:
                    self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM game_sessions').fetchone()[0], 1)
                    self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM branch_nodes').fetchone()[0], 1)

    def test_catalog_creation_reload_and_rejected_cross_role_entry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            with TestClient(create_app(ROOT / 'content/packages', Path(directory) / 'sessions.sqlite', play=True)) as client:
                catalog = client.get('/api/v1/packages/taixu-relics-part1/0.1.2').json()
                self.assertTrue(catalog['package']['context_preview']['available'])
                self.assertEqual(len(catalog['characters']), 3)
                expected_art = {'陆照临': 'gate-detail-v1', '顾长离': 'trial-detail-v1', '叶观澜': 'gallery-detail-v1'}
                for character in catalog['characters']:
                    entry = next(e for e in catalog['entries'] if e['id'] == character['defaultEntryPointId'])
                    self.assertEqual(entry['opening_image']['id'], expected_art[character['name']])
                    self.assertEqual(client.get(entry['opening_image']['url']).status_code, 200)
                    payload = {'package': {'package_id': 'taixu-relics-part1', 'version': '0.1.2'},
                               'entry_point_id': character['defaultEntryPointId'],
                               'source_character_id': character['id'], 'identity_opening': True, 'request_id': 'opening-' + character['id']}
                    response = client.post('/api/v1/sessions', json=payload)
                    self.assertEqual(response.status_code, 201, response.text)
                    result = response.json()
                    sid, branch = result['session']['id'], result['branch']
                    self.assertIn('openingContext', branch)
                    restored = client.post('/api/v1/sessions', json=payload).json()
                    self.assertEqual(restored['branch']['id'], branch['id'])
                    self.assertEqual(len(client.get(f'/api/v1/sessions/{sid}/branches').json()['branches']), 1)
                    saved = client.get(f"/api/v1/sessions/{sid}/branches/{branch['id']}").json()
                    self.assertEqual(saved['branchState'], branch['branchState'])
                    self.assertEqual(saved['openingContext'], branch['openingContext'])
                    other = next(c for c in catalog['characters'] if c['id'] != character['id'])
                    response = client.post('/api/v1/sessions', json={**payload, 'entry_point_id': other['defaultEntryPointId']})
                    self.assertEqual(response.status_code, 409)
