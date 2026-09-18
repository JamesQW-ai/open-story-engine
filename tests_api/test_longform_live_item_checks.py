"""Offline safeguards for the optional paid item-contract evaluation."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from open_story_engine.llm import Completion, OpenAICompatibleGateway
from test_support.live_item_dependencies import build_cases, run_jobs
from test_support.longform import ROOT, longform_cases


class LongformLiveItemChecks(unittest.TestCase):
    def setUp(self):
        fixtures = [json.loads(p.read_text()) for p in (ROOT / 'test_support/fixtures').glob('item-dependencies-*.json')]
        self.fixture = next(f for f in fixtures if f['package_id'] == self.case['package_id'])

    def test_fixture_binding_and_dry_run_never_reach_provider(self):
        with patch.object(OpenAICompatibleGateway, '_request', side_effect=AssertionError('offline call')):
            bad = dict(self.fixture, sha256='wrong')
            with self.assertRaisesRegex(ValueError, '摘要'):
                build_cases(self.case, bad)
            jobs = build_cases(self.case, self.fixture)
        self.assertEqual(len(jobs), 5)
        # Observation must not receive the player's desired result contract.
        observed = json.loads(next(j['messages'][1]['content'] for j in jobs if j['id'] == 'observe_destroyed'))
        self.assertNotIn('input', observed)
        self.assertNotIn('requirements', observed)
        self.assertNotIn('resultContract', observed)
        gateway = Mock()
        gateway.complete_json.side_effect = AssertionError('dry-run made a paid call')
        record = dict(package_id=self.case['package_id'], checks=[])
        with TemporaryDirectory() as temp, patch('builtins.print'):
            run_jobs(jobs, gateway, Path(temp), record, lambda: None, dry_run=True)
        gateway.complete_json.assert_not_called()
        self.assertEqual([c['status'] for c in record['checks']], ['fixture_valid'] * 5)

    def test_rejected_first_attempt_preserves_response_usage_and_never_retries(self):
        jobs = build_cases(self.case, self.fixture)
        raw = json.dumps({'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16}})
        gateway = Mock()
        gateway.complete_json.return_value = Completion('{}', raw, [{'outcome': 'completed'}])
        record = dict(package_id=self.case['package_id'], checks=[])
        before = copy.deepcopy([job['messages'] for job in jobs])
        with TemporaryDirectory() as temp, patch('builtins.print'):
            run_jobs(jobs, gateway, Path(temp), record, lambda: None)
            self.assertEqual(gateway.complete_json.call_count, 5)
            self.assertTrue(all(c['status'] == 'failed' for c in record['checks']))
            for check, messages in zip(record['checks'], before):
                artifact = json.loads((Path(temp) / check['artifact']).read_text())
                self.assertEqual(artifact['messages'], messages)
                self.assertEqual(artifact['response'], '{}')
                self.assertEqual(artifact['raw_response'], raw)
                self.assertIsNotNone(check['usage'])
                self.assertNotIn('validated', artifact)

    def test_cli_preflight_covers_corpus_and_refuses_overwrite_or_insufficient_budget(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / 'report'
            command = [sys.executable, '-B', '-m', 'test_support.live_item_dependencies', '--output', str(output)]
            # A budget smaller than one complete corpus run must fail before loading a live gateway.
            result = subprocess.run([*command, '--max-calls', '4'], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
            result = subprocess.run([*command, '--dry-run'], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['provider_calls'], 0)
            self.assertTrue(report['passed'])
            self.assertEqual({n['package_id'] for n in report['novels']}, {c['package_id'] for c in longform_cases()})
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            result = subprocess.run([*command, '--dry-run'], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformLiveItems_' + case['package_id'],
                                                        (LongformLiveItemChecks,), {'case': case})))
    return suite
