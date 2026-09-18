"""Scene image lifecycle and branch-local, non-spoiling relationship evidence."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, MagicMock, patch

from open_story_engine.api_illustrations import IllustrationService, ImageGateway, reading_sections, image_bytes, illustration_prompt
from open_story_engine.api_relationships import known_relationships
from open_story_engine.api_read import ReadError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6bAAAAABJRU5ErkJggg==')


def media_service(read, directory, gateway):
    read.store.return_value = MagicMock()
    read.session.return_value = {'storyPackageId': 'test', 'storyPackageVersion': '0.1.0'}
    library = Mock()
    library.resolve.return_value = None
    library.live_policy.return_value = {'prompt': 'scene'}
    return IllustrationService(read, directory, gateway, library)


class MediaTests(unittest.TestCase):
    def test_picture_uses_one_moment_with_consistent_cast_not_future_paragraph(self):
        journal = {'role_name': '沈执事', 'location': '山门', 'people': [{'name': n, 'identity': '提着无芯灯的来客' if n == '叶观澜' else ''} for n in ('沈执事', '叶观澜', '陆照临', '顾长离')]}
        prompt = illustration_prompt('你停在石道，叶观澜把引路灯举高，照亮你脚下的积水。\n\n顾长离从门里走出来，陆照临扶住她。', journal)
        self.assertIn('只允许1个人出镜：叶观澜', prompt)
        self.assertIn('提着无芯灯的来客', prompt)
        self.assertNotIn('顾长离', prompt)
        self.assertNotIn('陆照临', prompt)
        voice = illustration_prompt('你站在铁门外，顾长离的声音从门内传来，门仍关着，视线被挡住。', journal)
        self.assertIn('只允许0个人出镜', voice)
        self.assertNotIn('女性青年', voice)
        self.assertIn('当前场景：山门', illustration_prompt('你走到守门弟子面前，请他如实上报。', journal))

    def test_reviewed_stance_is_branch_local_and_keeps_unseen_people_hidden(self):
        people = [{'id': 'chen', 'name': '沈执事', 'first_page': 1}, {'id': 'jiang', 'name': '叶观澜', 'first_page': 1}]
        quote = '叶观澜看着你，要求留下原始记录，配合后续核查。'
        node = {'sequence': 1, 'narrativeText': quote, 'readerOutcome': {'relationships': [
            {'source': '叶观澜', 'target': '沈执事', 'label': '配合核查', 'evidence': quote},
            {'source': '顾长离', 'target': '沈执事', 'label': '对立', 'evidence': quote}]}}
        result = known_relationships([node], people, '沈执事')
        self.assertEqual([(r['source'], r['target'], r['label']) for r in result], [('jiang', 'chen', '配合核查')])
        self.assertEqual(known_relationships([node], [people[0], {**people[1], 'first_page': 3}], '沈执事'), [])
        self.assertEqual(known_relationships([], people, '沈执事'), [])

    def test_image_generation_is_async_deduplicated_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            read, gateway = Mock(), Mock()
            read.branch_view.return_value = {'narrativeText': '你扶住顾长离，站在灯下。'}
            read.journey.return_value = {'role_name': '陆照临', 'people': [{'name': '陆照临', 'identity': '初到山门的年轻人'}]}
            gateway.available, gateway.model = True, 'image-model'
            entered, release = Event(), Event()
            def generate(prompt):
                self.assertIn('你扶住顾长离', prompt)
                entered.set()
                self.assertTrue(release.wait(3))
                return PNG, 'image/png'
            gateway.generate.side_effect = generate
            service = media_service(read, directory, gateway)
            try:
                self.assertTrue(service.view('s', 'b')['can_generate'])
                self.assertFalse(list(Path(directory).iterdir()))
                self.assertIn(service.ensure('s', 'b', subscriber='tab', draw=True)['items'][0]['status'], ('queued', 'generating'))
                self.assertTrue(entered.wait(1))
                service.ensure('s', 'b')
                self.assertEqual(gateway.generate.call_count, 1)
            finally:
                release.set()
                service._pool.shutdown(wait=True)
            self.assertEqual(service.view('s', 'b')['items'][0]['status'], 'ready')
            restored = media_service(read, directory, gateway)
            self.addCleanup(restored.close)
            self.assertEqual(restored.ensure('s', 'b')['items'][0]['status'], 'ready')
            self.assertEqual(gateway.generate.call_count, 1)
            self.assertEqual(restored.asset('s', 'b', 0)[0].read_bytes(), PNG)
            gateway.available = False
            gateway.model = ''
            self.assertEqual(restored.view('s', 'b')['items'][0]['status'], 'ready')
            gateway.available = True
            # Sibling branches are not the same illustration/cache identity.
            self.assertTrue(restored.view('s', 'sibling')['can_generate'])
            read.branch_view.side_effect = ReadError(404, 'branch_not_found', '不存在')
            with self.assertRaises(ReadError): restored.asset('wrong-session', 'b', 0)

    def test_disabled_and_failed_images_do_not_retry_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            read, gateway = Mock(), Mock()
            read.branch_view.return_value = {'narrativeText': '你沿门边走。'}
            read.journey.return_value = {'role_name': '陆照临', 'people': []}
            gateway.available, gateway.model = False, 'image-model'
            service = media_service(read, directory, gateway)
            self.assertEqual(service.ensure('s', 'b'), {'available': False, 'items': [], 'can_generate': False})
            gateway.generate.assert_not_called()
            self.assertFalse(list(Path(directory).iterdir()))
            gateway.available = True
            gateway.generate.side_effect = RuntimeError('provider unavailable')
            service.ensure('s', 'b', subscriber='tab', draw=True)
            service._pool.shutdown(wait=True)
            self.assertEqual(service.view('s', 'b')['items'][0]['status'], 'failed')
            service.ensure('s', 'b')
            self.assertEqual(gateway.generate.call_count, 1)

    def test_gateway_contract_and_invalid_image_response(self):
        with patch.dict('os.environ', {'STORY_IMAGE_BASE_URL': 'https://images.example/v1', 'STORY_IMAGE_API_KEY': 'test-key', 'STORY_IMAGE_MODEL': 'gpt-image-test'}):
            gateway = ImageGateway()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'data': [{'b64_json': base64.b64encode(PNG).decode()}]}).encode()
        with patch('open_story_engine.api_illustrations.urlopen', return_value=response) as call:
            self.assertEqual(gateway.generate('绘制门边的灯'), (PNG, 'image/png'))
            request = call.call_args.args[0]
            self.assertEqual(request.full_url, 'https://images.example/v1/images/generations')
            self.assertEqual(json.loads(request.data)['prompt'], '绘制门边的灯')
            self.assertNotIn('response_format', json.loads(request.data))
        with self.assertRaises(ValueError): image_bytes(b'<html>provider failure</html>')

    def test_reading_boundaries_preserve_prose_and_limit_picture_count(self):
        text = ('你走到门边。\n\n' * 250) + '雨' * 1200
        sections = reading_sections(text)
        self.assertEqual(''.join(s.replace('\n', '') for s in sections), text.replace('\n', ''))
        read, gateway = Mock(), Mock()
        read.branch_view.return_value = {'narrativeText': text}
        with tempfile.TemporaryDirectory() as directory:
            service = media_service(read, directory, gateway)
            self.addCleanup(service.close)
            self.assertLessEqual(len(service._scenes('s', 'b')), 1)

    def test_long_scene_without_preset_allows_explicit_private_draw(self):
        read, gateway = Mock(), Mock(available=True, model='image-model')
        read.branch_view.side_effect = lambda sid, bid: {'parentId': 'root' if bid == 'b' else None, 'narrativeText': '雨' * 700}
        with tempfile.TemporaryDirectory() as directory:
            service = media_service(read, directory, gateway)
            self.addCleanup(service.close)
            service.library.live_policy.return_value = None
            self.assertTrue(service.view('s', 'b')['can_generate'])
            self.assertFalse(service.view('s', 'root')['can_generate'])
            gateway.generate.return_value = (PNG, 'image/png')
            result = service.ensure('s', 'b', subscriber='reader', draw=True)
            self.assertIn(result['items'][0]['status'], ('queued', 'generating'))
            service._pool.shutdown(wait=True)
            self.assertEqual(service.view('s', 'b')['items'][0]['status'], 'ready')

    def test_graph_only_connects_evidenced_people_on_current_route(self):
        people = [{'id': 'xu', 'name': '陆照临', 'first_page': 1}, {'id': 'tang', 'name': '顾长离', 'first_page': 2}, {'id': 'chen', 'name': '沈执事', 'first_page': 3}]
        root = {'sequence': 0, 'narrativeText': '你想起顾长离。你告诉沈执事往事。'}
        self.assertEqual(known_relationships([root], people, '陆照临'), [])
        saved = {'sequence': 1, 'narrativeText': '你扶住顾长离。'}
        links = known_relationships([root, saved], people, '陆照临')
        self.assertEqual([(l['source'], l['target'], l['label']) for l in links], [('xu', 'tang', '协助')])
        self.assertEqual(links[0]['page'], 2)
        other = {'sequence': 1, 'narrativeText': '你没有拦住顾长离。假如你救出顾长离。'}
        self.assertEqual(known_relationships([root, other], people, '陆照临'), [])
        friend = {'sequence': 1, 'narrativeText': '你看见顾长离这位原本的朋友。'}
        self.assertEqual(known_relationships([friend], people, '陆照临')[0]['label'], '朋友')
