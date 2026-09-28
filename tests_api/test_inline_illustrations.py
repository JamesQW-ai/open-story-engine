"""Inline art uses the saved longform paragraph adjacent to its display slot."""
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from http.client import HTTPResponse

from open_story_engine.api_illustrations import (CJK, illustration_moment, long_scene_policy, reading_sections,
                                               scheduled_scene, scene_prompt, image_response_bytes, ImageGateway)
from test_support.longform import longform_cases
from tests_api.test_story_media import PNG, media_service


class InlineIllustrationTests(unittest.TestCase):
    def test_very_short_pages_still_trigger_fourth_and_seventh_pages(self):
        source = longform_cases()[0]['source'].read_text()
        nodes = [dict(narrativeText=source[i:i+180]) for i in range(0, 1260, 180)]
        self.assertEqual([i + 1 for i in range(len(nodes)) if scheduled_scene(nodes[:i+1])], [4, 7])

    def test_every_long_page_is_automatic_even_immediately_after_an_image(self):
        source = longform_cases()[0]['source'].read_text()
        nodes = [dict(narrativeText=source[i:i+3000]) for i in range(0, 9000, 3000)]
        self.assertTrue(all(scheduled_scene(nodes[:i+1]) for i in range(len(nodes))))
        with tempfile.TemporaryDirectory() as directory:
            read, gateway = Mock(), Mock(available=True)
            read.branch_view.return_value = nodes[1]
            service = media_service(read, directory, gateway)
            self.addCleanup(service.close)
            read.store.return_value.__enter__.return_value.lineage.return_value = nodes[:2]
            self.assertTrue(service.view('s', 'long-second')['automatic'])

    def test_consumed_published_image_does_not_suppress_new_private_picture(self):
        source = longform_cases()[0]['source'].read_text()
        read, gateway = Mock(), Mock(available=True, model='test-image', usage=None)
        read.branch_view.return_value = dict(parentId='root', narrativeText=source[:3000], branchState={})
        gateway.generate.return_value = PNG, 'image/png'
        with tempfile.TemporaryDirectory() as directory:
            service = media_service(read, directory, gateway)
            self.addCleanup(service.close)
            service.library.resolve.return_value = dict(id='art', asset_key='one-image', source='published', url='/art', alt='素材')
            self.assertEqual(service.ensure('s', 'opening', subscriber='a')['items'][0]['source'], 'published')
            self.assertTrue(service.ensure('s', 'later', subscriber='b')['can_generate'])
            service.ensure('s', 'later', subscriber='b', draw=True)
            service._pool.shutdown(wait=True)
            self.assertEqual(service.view('s', 'later')['items'][0]['source'], 'private')
            self.assertEqual(service.view('s', 'later')['items'][0]['status'], 'ready')
            service.shown('s', 'later', 'b', 20, source='private')
            self.assertTrue(next(iter(service._jobs.values()))['displayed'])
            gateway.generate.assert_called_once()

    def test_queue_time_is_deducted_from_provider_budget_and_expired_jobs_never_spend(self):
        read = Mock()
        read.branch_view.return_value = dict(parentId='root', narrativeText='你查看眼前的石壁。', branchState={})
        gateway = ImageGateway()
        gateway.base_url, gateway.api_key, gateway.model = 'https://test.invalid', 'test', 'gpt-image-test'
        gateway.generate = Mock(return_value=(PNG, 'image/png'))
        with tempfile.TemporaryDirectory() as directory:
            service = media_service(read, directory, gateway)
            try:
                with patch.object(service._pool, 'submit'):
                    service.ensure('s', 'first', subscriber='tab', draw=True)
                job = next(iter(service._jobs.values()))
                job['created'] = time.monotonic() - 70
                service._generate(job['key'], '一幕')
                self.assertGreater(gateway.generate.call_args.kwargs['timeout'], 0)
                self.assertLessEqual(gateway.generate.call_args.kwargs['timeout'], 5)
                with patch.object(service._pool, 'submit'):
                    service.ensure('s', 'expired', subscriber='tab2', draw=True)
                expired = next(j for j in service._jobs.values() if j['bid'] == 'expired')
                expired['created'] = time.monotonic() - 76
                service._generate(expired['key'], '过期')
                self.assertEqual(expired['status'], 'cancelled')
                self.assertEqual(expired['provider_calls'], 0)
                gateway.generate.assert_called_once()
            finally:
                service.close()

    def test_keepalive_bytes_cannot_extend_total_image_deadline(self):
        response = Mock(spec=HTTPResponse)
        response.fp = Mock()
        response.read1.return_value = b' '
        connection = Mock()
        connection.__enter__ = Mock(return_value=response)
        connection.__exit__ = Mock(return_value=False)
        with patch('open_story_engine.api_illustrations.urlopen', return_value=connection), \
                patch('open_story_engine.api_illustrations.time.monotonic', side_effect=[0, 1, 44, 46]):
            with self.assertRaises(TimeoutError):
                image_response_bytes('https://test.invalid/image', 1024, 45)
        self.assertEqual(response.read1.call_count, 2)
        self.assertEqual([call.args[0] for call in response.fp.raw._sock.settimeout.call_args_list], [44, 1])

    def test_actual_continuous_record_has_sparse_illustration_opportunities(self):
        source = longform_cases()[0]['source'].read_text()
        # Contiguous excerpts of the official longform, never repeated padding.
        nodes = [dict(narrativeText=source[i:i+900]) for i in range(0, 9000, 900)]
        chosen = [i for i in range(1, len(nodes)) if scheduled_scene(nodes[:i+1])]
        self.assertGreaterEqual(len(chosen), 2)
        self.assertLess(len(chosen), len(nodes) // 2)
        self.assertTrue(all(b-a >= 3 for a,b in zip(chosen, chosen[1:])))
        self.assertIsNone(scheduled_scene(nodes[:2]))

    def test_short_selected_scene_prompt_uses_its_middle_and_stays_bounded(self):
        source = longform_cases()[0]['source'].read_text()
        nodes = [dict(narrativeText=source[i:i+900]) for i in range(0, 3600, 900)]
        text = nodes[-1]['narrativeText']
        policy = scheduled_scene(nodes)
        self.assertIsNotNone(policy)
        node = dict(narrativeText=text, _illustrationSchedule=policy)
        prompt = scene_prompt(node, policy)
        self.assertIn(illustration_moment(text, True), prompt)
        self.assertLess(len(prompt), 2000)

    def test_real_longforms_use_middle_paragraph_and_opening_threshold(self):
        for case in longform_cases():
            with self.subTest(package=case['package_id']):
                text = case['source'].read_text()[:3000]
                paragraphs = [p for section in reading_sections(text) for p in section.split('\n')]
                moment = illustration_moment(text)
                self.assertIn(moment, paragraphs[:-1])
                self.assertNotEqual(moment, paragraphs[0])
                self.assertIsNotNone(long_scene_policy({'narrativeText': text}))
                self.assertIsNone(long_scene_policy({'narrativeText': text[:999]}))
                # Exact boundary uses real prose, without template padding.
                boundary = next(i for i in range(len(text)) if len(CJK.findall(text[:i])) == 1000)
                self.assertIsNone(long_scene_policy({'narrativeText': text[:boundary - 1]}))
                self.assertIsNotNone(long_scene_policy({'narrativeText': text[:boundary]}))

    def test_async_generation_and_public_matching_use_the_same_local_moment(self):
        text = longform_cases()[0]['source'].read_text()[:3000]
        node = {'parentId': 'root', 'narrativeText': text, 'branchState': {}}
        read = Mock()
        read.branch_view.return_value = node
        gateway = Mock(available=True, model='image-model')
        gateway.generate.return_value = PNG, 'image/png'
        with tempfile.TemporaryDirectory() as directory:
            service = media_service(read, directory, gateway)
            try:
                service.ensure('temporary', 'branch', subscriber='tab', draw=True)
                service._pool.shutdown(wait=True)
                moment = illustration_moment(text)
                self.assertEqual(service.library.resolve.call_args.args[2]['sceneNarrativeText'], moment)
                self.assertIn(moment, gateway.generate.call_args.args[0])
                self.assertNotIn(text[-300:], gateway.generate.call_args.args[0])
                service.ensure('temporary', 'branch', subscriber='tab2', draw=True)
                gateway.generate.assert_called_once()
                self.assertNotIn('sceneNarrativeText', node)
            finally:
                service.close()
