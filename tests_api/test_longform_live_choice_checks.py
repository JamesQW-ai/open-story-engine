"""Offline coverage and spending safeguards for the optional live menu probe."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from open_story_engine.llm import Completion, OpenAICompatibleGateway
from test_support.longform import ROOT, longform_cases
from test_support.live_choice_availability import build_cases
from test_support.live_item_dependencies import run_jobs


class LongformLiveChoiceChecks(unittest.TestCase):
    def setUp(self):
        fixtures = [json.loads(p.read_text()) for p in (ROOT / 'test_support/fixtures').glob('choice-availability-*.json')]
        self.fixture = next(f for f in fixtures if f['package_id'] == self.case['package_id'])

    def test_preflight_binds_source_and_covers_every_identity_without_provider_calls(self):
        with patch.object(OpenAICompatibleGateway, '_request', side_effect=AssertionError('preflight paid call')):
            for field in ('version', 'sha256', 'package_id'):
                with self.assertRaisesRegex(ValueError, '摘要'):
                    build_cases(self.case, dict(self.fixture, **{field: 'wrong'}))
            for scenarios in (self.fixture['scenarios'][:-1], self.fixture['scenarios'] * 2):
                with self.assertRaisesRegex(ValueError, '全部预设身份'):
                    build_cases(self.case, dict(self.fixture, scenarios=scenarios))
            before = copy.deepcopy(self.fixture)
            jobs = build_cases(self.case, self.fixture)
            self.assertEqual(self.fixture, before)
            self.assertEqual(len(jobs), len(self.fixture['scenarios']))
            for job, scenario in zip(jobs, self.fixture['scenarios']):
                context = json.loads(job['messages'][1]['content'])
                target = next(p for p in context['people'] if p['id'] == scenario['target_id'])
                self.assertIs(target['available'], scenario['available'])

    def test_rejected_menu_keeps_first_response_and_does_not_retry(self):
        jobs = build_cases(self.case, self.fixture)
        gateway = Mock()
        gateway.complete_json.return_value = Completion('{}', '{"usage":{"total_tokens":17}}', [])
        record = dict(package_id=self.case['package_id'], checks=[])
        with TemporaryDirectory() as temp, patch('builtins.print'):
            run_jobs(jobs, gateway, Path(temp), record, lambda: None)
            self.assertEqual(gateway.complete_json.call_count, len(jobs))
            for check in record['checks']:
                self.assertEqual(check['status'], 'failed')
                artifact = json.loads((Path(temp) / check['artifact']).read_text())
                self.assertEqual(artifact['response'], '{}')
                self.assertIn('raw_response', artifact)
                self.assertNotIn('validated', artifact)

    def test_cli_budget_dry_run_and_overwrite_guards(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / 'results'
            command = [sys.executable, '-B', '-m', 'test_support.live_choice_availability', '--output', str(output)]
            result = subprocess.run([*command, '--max-calls', '1'], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
            result = subprocess.run([*command, '--dry-run'], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['provider_calls'], 0)
            self.assertTrue(report['passed'])
            self.assertEqual({n['package_id'] for n in report['novels']}, {c['package_id'] for c in longform_cases()})
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            result = subprocess.run([*command, '--dry-run'], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformLiveChoices_' + case['package_id'],
                                                        (LongformLiveChoiceChecks,), {'case': case})))
    return suite
