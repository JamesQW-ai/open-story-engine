import unittest

from test_support.context_projection_replay import _projection_boundary_violations


class ContextProjectionReplayTests(unittest.TestCase):
    def test_recursive_scan_rejects_hidden_state_and_cross_branch_evidence(self):
        projection = {
            "authoritativeState": {"nested": {"branchLedger": {}}},
            "allowedEvidence": [
                {"visibility": "author_truth", "branchId": "other-branch"},
            ],
        }
        violations = _projection_boundary_violations(projection, "current-branch")
        self.assertIn("/authoritativeState/nested/branchLedger", violations)
        self.assertIn("/allowedEvidence/0/visibility=author_truth", violations)
        self.assertIn("/allowedEvidence/0/branchId=other-branch", violations)

    def test_recursive_scan_accepts_player_visible_current_branch_projection(self):
        projection = {
            "authoritativeState": {"playerLocationId": "location_open_gate"},
            "allowedEvidence": [
                {"visibility": "player_known", "branchId": "current-branch"},
                {"visibility": "public_world_fact", "branchId": "package"},
            ],
        }
        self.assertEqual(
            _projection_boundary_violations(projection, "current-branch"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
