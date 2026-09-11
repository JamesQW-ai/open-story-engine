"""Scene image lifecycle and branch-local, non-spoiling relationship evidence."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from open_story_engine.api_illustrations import IllustrationService, ImageGateway, reading_sections, image_bytes
from open_story_engine.api_relationships import known_relationships
from open_story_engine.api_read import ReadError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6bAAAAABJRU5ErkJggg==')


class MediaTests(unittest.TestCase):
    def test_image_generation_is_async_deduplicated_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            read, gateway = Mock(), Mock()
            read.branch_view.return_value = {'narrativeText': '你扶住唐栖，站在灯下。'}
            read.journey.return_value = {'role_name': '许川', 'people': [{'name': '许川', 'identity': '来寻人的朋友'}]}
            gateway.available, gateway.model = True, 'image-model'
            entered, release = Event(), Event()
            def generate(prompt):
                self.assertIn('你扶住唐栖', prompt)
                entered.set()
                self.assertTrue(release.wait(3))
                return PNG, 'image/png'
            gateway.generate.side_effect = generate
            service = IllustrationService(read, directory, gateway)
            try:
                self.assertEqual(service.view('s', 'b')['items'][0]['status'], 'idle')
                self.assertFalse(list(Path(directory).iterdir()))
                self.assertEqual(service.ensure('s', 'b')['items'][0]['status'], 'pending')
                self.assertTrue(entered.wait(1))
                service.ensure('s', 'b')
                self.assertEqual(gateway.generate.call_count, 1)
            finally:
                release.set()
                service._pool.shutdown(wait=True)
            self.assertEqual(service.view('s', 'b')['items'][0]['status'], 'ready')
            restored = IllustrationService(read, directory, gateway)
            self.addCleanup(restored.close)
            self.assertEqual(restored.ensure('s', 'b')['items'][0]['status'], 'ready')
            self.assertEqual(gateway.generate.call_count, 1)
            self.assertEqual(restored.asset('s', 'b', 0)[0].read_bytes(), PNG)
            gateway.available = False
            gateway.model = ''
            self.assertEqual(restored.view('s', 'b')['items'][0]['status'], 'ready')
            gateway.available = True
            # Sibling branches are not the same illustration/cache identity.
            self.assertEqual(restored.view('s', 'sibling')['items'][0]['status'], 'idle')
            read.branch_view.side_effect = ReadError(404, 'branch_not_found', '不存在')
            with self.assertRaises(ReadError): restored.asset('wrong-session', 'b', 0)

    def test_disabled_and_failed_images_do_not_retry_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            read, gateway = Mock(), Mock()
            read.branch_view.return_value = {'narrativeText': '你沿门边走。'}
            read.journey.return_value = {'role_name': '许川', 'people': []}
            gateway.available, gateway.model = False, 'image-model'
            service = IllustrationService(read, directory, gateway)
            self.assertEqual(service.ensure('s', 'b'), {'available': False, 'items': []})
            gateway.generate.assert_not_called()
            self.assertFalse(list(Path(directory).iterdir()))
            gateway.available = True
            gateway.generate.side_effect = RuntimeError('provider unavailable')
            service.ensure('s', 'b')
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
            service = IllustrationService(read, directory, gateway)
            self.addCleanup(service.close)
            self.assertLessEqual(len(service._scenes('s', 'b')), 4)

    def test_graph_only_connects_evidenced_people_on_current_route(self):
        people = [{'id': 'xu', 'name': '许川', 'first_page': 1}, {'id': 'tang', 'name': '唐栖', 'first_page': 2}, {'id': 'chen', 'name': '陈砚', 'first_page': 3}]
        root = {'sequence': 0, 'narrativeText': '你想起唐栖。你告诉陈砚往事。'}
        self.assertEqual(known_relationships([root], people, '许川'), [])
        saved = {'sequence': 1, 'narrativeText': '你扶住唐栖。'}
        links = known_relationships([root, saved], people, '许川')
        self.assertEqual([(l['source'], l['target'], l['label']) for l in links], [('xu', 'tang', '协助')])
        self.assertEqual(links[0]['page'], 2)
        other = {'sequence': 1, 'narrativeText': '你没有拦住唐栖。假如你救出唐栖。'}
        self.assertEqual(known_relationships([root, other], people, '许川'), [])
        friend = {'sequence': 1, 'narrativeText': '你看见唐栖这位原本的朋友。'}
        self.assertEqual(known_relationships([friend], people, '许川')[0]['label'], '朋友')
