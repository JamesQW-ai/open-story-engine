from unittest.mock import patch

from tests_py import test_context_assertion_reasoning as original
from test_support import context_assertion_reasoning_remainder_eval as candidate


class ReasoningRemainderTests(original.ReasoningProbeTests):
    expected_cap=8192
    expected_cases=36

    @classmethod
    def setUpClass(cls):
        cls.runner_patch=patch.object(original,'run',candidate)
        cls.runner_patch.start()
        cls.addClassCleanup(cls.runner_patch.stop)
        super().setUpClass()

    def test_remainder_never_repeats_capacity_probe(self):
        self.assertFalse(set(candidate.CASE_IDS)&set(candidate.baseline.CASE_IDS))
        self.assertEqual(len(set(candidate.CASE_IDS)|set(candidate.baseline.CASE_IDS)),48)
