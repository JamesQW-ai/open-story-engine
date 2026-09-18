"""Author diagnostics reuse runtime rules and never modify long-novel assets."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from open_story_engine.scene_library import SceneLibrary
from test_support.check_scene_library import check_case
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_scene_library import LongformSceneLibraryTests


class LongformSceneLibraryCheckTests(unittest.TestCase):
    save = LongformSceneLibraryTests.save

    def setUp(self):
        LongformSceneLibraryTests.setUp(self)
        self.manifest['schema_version'] = 'story-scene-library/1'
        self.save()

    def check(self):
        result = check_case(self.case, self.library.root)
        json.dumps(result, allow_nan=False)
        return result

    def test_valid_report_is_read_only_and_omits_prose_and_prompts(self):
        self.manifest['live_rules'] = [dict(self.card, prompt='private prompt sentinel')]
        self.save()
        before = {str(p): p.read_bytes() for p in self.base.iterdir()}
        result = self.check()
        self.assertEqual(result['status'], 'valid')
        self.assertEqual((result['checked_assets'], result['checked_live_rules']), (1, 1))
        self.assertEqual(result['issues'], [])
        output = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('private prompt sentinel', output)
        self.assertNotIn(self.node['narrativeText'], output)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.base.iterdir()})

    def test_reports_all_field_errors_with_stable_locations_and_runtime_agreement(self):
        bad = dict(self.card, required_text=['['], alt='', priority=float('inf'),
                   characters={self.cid: {'outcome': {'status': 'unknown'}}})
        self.manifest.update(assets=[bad, dict(self.card)], live_rules=[dict(self.card, prompt=None)])
        self.save()
        result = self.check()
        found = {(issue['path'], issue['code']) for issue in result['issues']}
        self.assertTrue({('/assets/0/required_text/0', 'invalid_regex'), ('/assets/0/alt', 'invalid_text'),
                         ('/assets/0/priority', 'invalid_priority'), ('/assets/0/id', 'duplicate_id'),
                         ('/assets/1/id', 'duplicate_id'), ('/live_rules/0/prompt', 'invalid_prompt'),
                         (f'/assets/0/characters/{self.cid}/appearance_version', 'invalid_appearance'),
                         (f'/assets/0/characters/{self.cid}/outcome/status', 'invalid_outcome')}.issubset(found))
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(result['checked_assets'], 0)
        self.assertEqual(SceneLibrary._assets(self.manifest), [])
        self.assertIsNone(self.library.live_policy(self.case['package_id'], self.case['version'], dict(self.node, parentId=self.parent)))

    def test_missing_unreadable_and_invalid_roots_have_actionable_diagnostics(self):
        self.manifest_path.unlink()
        self.assertEqual(self.check()['issues'][0]['code'], 'manifest_missing')
        self.assertFalse(self.manifest_path.exists())
        for data, code in ((b'\xff', 'manifest_unreadable'), (b'[]', 'invalid_object'), (b'null', 'invalid_object')):
            self.manifest_path.write_bytes(data)
            result = self.check()
            self.assertEqual(result['issues'][0]['code'], code)
            self.assertEqual(self.manifest_path.read_bytes(), data)
        self.manifest_path.write_text('{\n  "assets": [\n}')
        error = self.check()['issues'][0]
        self.assertEqual((error['code'], error['line'], error['column']), ('invalid_json', 3, 1))
        self.manifest_path.unlink()
        self.manifest_path.symlink_to('manifest.json')
        self.assertEqual(self.check()['issues'][0]['code'], 'manifest_unreadable')

    def test_bindings_and_collection_errors_cannot_report_success(self):
        self.manifest.update(package_id='wrong', version='0.0.0', source_sha256='wrong', schema_version='unknown',
                             assets=None, live_rules={})
        self.save()
        result = self.check()
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual({i['path'] for i in result['issues']},
                         {'/package_id', '/version', '/source_sha256', '/schema_version', '/assets', '/live_rules'})
        self.assertIsNone(result['asset_count'])
        self.assertIsNone(result['live_rule_count'])
        self.manifest.update(assets=[None], live_rules=[False])
        self.save()
        found = {(issue['path'], issue['code']) for issue in self.check()['issues']}
        self.assertIn(('/assets/0', 'invalid_object'), found)
        self.assertIn(('/live_rules/0', 'invalid_object'), found)

    def test_files_report_missing_digest_path_and_loop_separately(self):
        (self.base / 'loop.png').symlink_to('loop.png')
        cases = [(dict(file='missing.png'), 'file', 'asset_missing'),
                 (dict(sha256='0' * 64), 'sha256', 'asset_digest_mismatch'),
                 (dict(file='../outside.png'), 'file', 'asset_path_outside'),
                 (dict(file='loop.png'), 'file', 'invalid_asset_path'),
                 (dict(file='.'), 'file', 'asset_path_outside')]
        for change, field, code in cases:
            with self.subTest(code=code):
                self.manifest['assets'] = [dict(self.card, **change)]
                self.save()
                result = self.check()
                self.assertEqual(result['checked_assets'], 0)
                self.assertEqual(result['issues'][0]['path'], '/assets/0/' + field)
                self.assertEqual(result['issues'][0]['code'], code)

    def test_cli_failure_success_and_unknown_package_exit_codes(self):
        command = [sys.executable, '-B', '-m', 'test_support.check_scene_library',
                   '--library-root', str(self.library.root), '--package-id', self.case['package_id']]
        before = {str(p): p.read_bytes() for p in self.base.iterdir()}
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['status'], 'valid')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.base.iterdir()})
        (self.base / 'scene.png').write_bytes(b'broken')
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        report = json.loads(run.stdout)
        self.assertEqual((report['book_count'], report['error_count']), (1, 1))
        self.assertEqual(report['books'][0]['issues'][0]['code'], 'asset_digest_mismatch')
        command[-1] = 'unlisted-book'
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn('不在达标官方长篇清单中', run.stderr)

    def test_default_cli_covers_every_eligible_official_long_novel(self):
        run = subprocess.run([sys.executable, '-B', '-m', 'test_support.check_scene_library'],
                             cwd=ROOT, capture_output=True, text=True)
        self.assertIn(run.returncode, (0, 1), run.stderr)
        report = json.loads(run.stdout)
        expected = {(c['package_id'], c['version']) for c in longform_cases()}
        self.assertEqual({(c['package_id'], c['version']) for c in report['books']}, expected)
        self.assertEqual(report['book_count'], len(expected))
        self.assertEqual(run.returncode, 1 if report['error_count'] else 0)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformSceneLibraryCheck_' + case['package_id'],
                                                        (LongformSceneLibraryCheckTests,), {'case': case})))
    return suite
