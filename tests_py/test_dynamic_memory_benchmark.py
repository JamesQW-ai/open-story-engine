"""Validate benchmark identity and scope, without timing thresholds in CI."""
import unittest

from open_story_engine import dynamic_memory as memory
from test_support.dynamic_memory_benchmark import fixture, run
from test_support.longform import longform_cases


class DynamicMemoryBenchmarkTests(unittest.TestCase):
    def test_fixture_is_reproducible_and_uses_distinct_official_text(self):
        for case in longform_cases():
            first, module, expected = fixture(case, 32)
            second, _, _ = fixture(case, 32)
            self.assertEqual(first['lineage'], second['lineage'])
            self.assertEqual(first['contract'], second['contract'])
            bodies = [n['narrativeText'] for n in first['lineage'][1:]]
            self.assertEqual(len(set(bodies)), 32)
            source = case['source'].read_text()
            self.assertTrue(all(body in source for body in bodies))
            selected, _, audit = memory.select(first, first['parent']['branchState'], module)
            self.assertEqual([m['sourceIds'][0] for m in selected], [expected])
            self.assertGreater(audit['lifecycle']['counts']['superseded'], 0)

    def test_report_keeps_measurements_separate_from_semantic_acceptance(self):
        report = run(lengths=(12,), repeats=2)
        self.assertFalse(report['semanticAcceptance'])
        self.assertFalse(report['databaseMeasured'])
        for row in report['rows']:
            self.assertGreaterEqual(row['novelCjk'], 100000)
            self.assertEqual(row['expectedSources'], row['selectedSources'])
            self.assertEqual(len(row['timingsMs']), 2)
            self.assertEqual(row['uniqueBodies'], 12)
            self.assertLessEqual(row['auditDetails'], memory.MAX_LIFECYCLE_DETAILS)

    def test_invalid_run_size_fails_instead_of_reporting_empty_success(self):
        for lengths, repeats in (((), 2), ((0,), 2), ((1,), 0)):
            with self.subTest(lengths=lengths, repeats=repeats), self.assertRaises(ValueError):
                run(lengths, repeats)
