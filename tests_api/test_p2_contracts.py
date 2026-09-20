"""P2 API/OpenAPI contract gates against the frozen P0 closeout checkpoint."""

import tempfile
import unittest
from pathlib import Path

from open_story_engine.api import create_app


ROOT = Path(__file__).resolve().parents[1]


class P2ContractGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ose-p2-contract-")
        cls.app = create_app(ROOT / "content/packages", Path(cls._tmp.name) / "contract.sqlite", play=True)
        cls.openapi = cls.app.openapi()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_journey_and_closeout_routes_have_named_response_models(self):
        paths = self.openapi["paths"]
        expected = {
            "/api/v1/sessions/{session_id}/journey": "JourneyView",
            "/api/v1/sessions/{session_id}/route-closure": "RouteClosurePreparation",
            "/api/v1/sessions/{session_id}/ending-proposals": "EndingProposalView",
            "/api/v1/sessions/{session_id}/ending-proposals/{proposal_id}/commit": "EndingCommitResponse",
        }
        for path, model in expected.items():
            operations = paths[path]
            schemas = [
                operation.get("responses", {}).get("200", {}).get("content", {})
                .get("application/json", {}).get("schema", {})
                for operation in operations.values()
            ]
            self.assertTrue(any(f"#/components/schemas/{model}" in str(schema) for schema in schemas), path)
        self.assertIn("$ref", paths["/api/v1/sessions/{session_id}/ending-proposals/{proposal_id}/commit"]["post"]["responses"]["200"]["content"]["application/json"]["schema"])

    def test_openapi_declares_terminal_receipt_and_error_enum(self):
        schemas = self.openapi["components"]["schemas"]
        self.assertIn("EndingCommitResponse", schemas)
        self.assertIn("NaturalEndingReceipt", schemas)
        self.assertIn("RouteErrorResponse", schemas)
        self.assertIn("ending_proposal_stale", str(schemas["RouteErrorDetail"]))
        self.assertEqual(schemas["EndingCommitResponse"]["required"], ["status", "receipt"])
        self.assertEqual(schemas["NaturalEndingReceipt"]["properties"]["ending_written"]["const"], True)

    def test_journey_and_receipt_fields_are_required(self):
        schemas = self.openapi["components"]["schemas"]
        self.assertTrue(set(("branch_id", "ledger_coverage", "status", "route_health")) <= set(schemas["JourneyView"]["required"]))
        self.assertTrue(set(("coverage", "outstanding", "cleared", "ending_written")) <= set(schemas["EarlyEndingReceipt"]["required"]))
        self.assertTrue(set(("proposal_id", "binding_digest")) <= set(schemas["NaturalEndingReceipt"]["required"]))

    def test_typescript_and_client_keep_the_named_terminal_contract(self):
        types = (ROOT / "web/src/api/types.ts").read_text()
        client = (ROOT / "web/src/api/client.ts").read_text()
        for declaration in ("Journey", "RouteClosure", "EndingReview", "EndingAudit", "EndingCommitResponse"):
            self.assertIn(f"{declaration}", types)
        for field in ("ledger_coverage", "route_health", "ending_written", "proposal_id", "binding_digest"):
            self.assertIn(field, types)
        self.assertIn("request<Journey>", client)
        self.assertIn("request<EndingCommitResponse>", client)
        self.assertIn("request<EndRouteResponse>", client)


if __name__ == "__main__":
    unittest.main()
