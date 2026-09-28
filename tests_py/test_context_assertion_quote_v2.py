"""Reuse the experiment boundary regressions for the isolated second prompt."""
from unittest.mock import patch

from tests_py import test_context_assertion_quote as original
from test_support import context_assertion_quote_v2_eval as candidate


class QuotePromptV2Tests(original.QuoteContractTests):
    @classmethod
    def setUpClass(cls):
        cls.runner_patch=patch.object(original,'run',candidate)
        cls.runner_patch.start()
        cls.addClassCleanup(cls.runner_patch.stop)
        super().setUpClass()

    def test_prompt_is_replaced_within_budget_and_references_unchanged(self):
        self.assertLess(len(candidate.PROMPT.read_text()),len(candidate.baseline.PROMPT.read_text()))
        self.assertEqual(candidate.FIXTURE_SHA256,candidate.baseline.FIXTURE_SHA256)
        c=self.fixture['cases'][0];e=self.fixture['scenes'][c['sceneKey']]
        self.assertNotEqual(candidate.messages(c,e)[0],candidate.baseline.messages(c,e)[0])
        self.assertEqual(candidate.messages(c,e)[1],candidate.baseline.messages(c,e)[1])
