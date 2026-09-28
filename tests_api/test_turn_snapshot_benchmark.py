"""Paired benchmark must preserve context and never call a model or initialize reads."""
import unittest
from unittest.mock import patch

from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadOnlySessionStore
from test_support.turn_snapshot_benchmark import run


class TurnSnapshotBenchmarkTests(unittest.TestCase):
    def test_paired_real_snapshot_has_identical_binding_and_memory(self):
        with patch.object(PlayService, '_generate_turn', side_effect=AssertionError('model path')), \
                patch.object(ReadOnlySessionStore, '_initialize', side_effect=AssertionError('read migration')):
            result = run((8,), 2)
        self.assertEqual(result['modelCalls'], 0)
        self.assertFalse(result['semanticAcceptance'])
        self.assertTrue(result['rows'])
        for row in result['rows']:
            self.assertTrue(row['databaseUnchanged'])
            self.assertEqual(row['lineageReads'], {'shared_reads': 1, 'repeated_reads_control': 3})
            self.assertEqual(len(row['selectedSources']), 1)
            for samples in row['timings'].values():
                self.assertEqual(len(samples), 2)
                for sample in samples:
                    self.assertAlmostEqual(sum(v for k, v in sample.items() if k != 'totalMs'), sample['totalMs'])

    def test_invalid_parameters_fail_before_setup(self):
        for kwargs in ({'lengths': ()}, {'lengths': (0,)}, {'repeats': 0}, {'profiles': ('invalid',)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run(**kwargs)
