from unittest.mock import patch

from tests_py import test_context_assertion_reasoning as original
from test_support import context_assertion_reasoning_capacity_eval as candidate


class ReasoningCapacityTests(original.ReasoningProbeTests):
    expected_cap=8192

    @classmethod
    def setUpClass(cls):
        cls.runner_patch=patch.object(original,'run',candidate)
        cls.runner_patch.start()
        cls.addClassCleanup(cls.runner_patch.stop)
        super().setUpClass()
