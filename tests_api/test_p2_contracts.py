"""P2 contract gates.

These are expected failures on the pre-P0 baseline. Once P0's route-closeout
models are frozen, an unexpected success turns this file into a migration
signal.
"""

import unittest
from pathlib import Path

from open_story_engine.api import create_app


ROOT = Path(__file__).resolve().parents[1]


class P2ContractGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(ROOT / "content/packages", ROOT / "data" / "p2-contract-test.sqlite", play=True)
        cls.openapi = cls.app.openapi()

    @unittest.expectedFailure
    def test_journey_and_closeout_routes_have_named_response_models(self):
        paths = self.openapi["paths"]
        expected = {
            "/api/v1/sessions/{session_id}/journey": "JourneyView",
            "/api/v1/sessions/{session_id}/route-closure": "RouteClosurePreparation",
            "/api/v1/sessions/{session_id}/ending-proposals": "EndingProposalView",
        }
        for path, model in expected.items():
            operations = paths[path]
            schemas = [
                operation.get("responses", {}).get("200", {}).get("content", {})
                .get("application/json", {}).get("schema", {})
                for operation in operations.values()
            ]
            self.assertTrue(any(f"#/components/schemas/{model}" in str(schema) for schema in schemas), path)

    @unittest.expectedFailure
    def test_openapi_declares_terminal_receipt_and_error_enum(self):
        schemas = self.openapi["components"]["schemas"]
        self.assertIn("EndingCommitResponse", schemas)
        self.assertIn("NaturalEndingReceipt", schemas)
        self.assertIn("RouteErrorResponse", schemas)
        self.assertIn("ending_proposal_stale", str(schemas["RouteErrorResponse"]))


if __name__ == "__main__":
    unittest.main()
