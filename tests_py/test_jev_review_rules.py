import unittest

from test_support.jev_review_rules import QUESTIONS, RULE_SET_VERSION, evaluate_answers, preflight_state


def answers(*, decision="allow", values=None):
    result = {"review_decision": {"type": "choice", "choice": decision}}
    for key, question in QUESTIONS.items():
        if key == "review_decision":
            continue
        default = 1.0 if key == "claim_supported" else 0.0
        result[key] = {"type": question["type"], "noul": (values or {}).get(key, default)}
    return result


class JevReviewRulesTests(unittest.TestCase):
    def test_clean_candidate_is_allowed_and_scores_are_observational(self):
        review = evaluate_answers(answers())

        self.assertEqual(review["ruleSetVersion"], RULE_SET_VERSION)
        self.assertEqual(review["decision"], "allow")
        self.assertEqual(review["layers"]["L3"]["scores"]["minimum_observational_score"], 100)
        self.assertTrue(review["layers"]["L3"]["scoreIsObservational"])

    def test_fatal_signal_overrides_direct_allow_and_score(self):
        review = evaluate_answers(
            answers(
                values={"hard_state_conflict": 0.9},
            )
        )

        self.assertEqual(review["decision"], "rewrite")
        self.assertEqual(review["reason"], "fatal_or_unsupported_patch_risk")
        self.assertEqual(review["reasonCodes"], ["hard_state_conflict"])
        self.assertEqual(review["layers"]["L0"]["status"], "rewrite")
        self.assertEqual(review["layers"]["L3"]["scores"]["minimum_observational_score"], 10)

    def test_bounded_anomaly_can_be_patched_without_fatal_signal(self):
        review = evaluate_answers(
            answers(values={"bounded_anomaly": 0.9})
        )

        self.assertEqual(review["decision"], "allow_with_patch")
        self.assertEqual(review["reason"], "bounded_anomaly_or_model_patch")
        self.assertEqual(review["reasonCodes"], ["bounded_anomaly"])

    def test_missing_patch_support_blocks_anomaly(self):
        review = evaluate_answers(
            answers(values={"bounded_anomaly": 0.9, "missing_patch_support": 0.9})
        )

        self.assertEqual(review["decision"], "rewrite")
        self.assertEqual(review["reasonCodes"], ["missing_patch_support"])

    def test_unsupported_concrete_claim_blocks_even_without_state_conflict(self):
        review = evaluate_answers(
            answers(values={"claim_supported": 0.1})
        )

        self.assertEqual(review["decision"], "rewrite")
        self.assertEqual(review["reasonCodes"], ["unsupported_claim"])
        self.assertEqual(review["layers"]["L0"]["status"], "rewrite")

    def test_incomplete_or_malformed_answers_reject(self):
        incomplete = answers()
        incomplete.pop("future_fact_leak")
        self.assertEqual(evaluate_answers(incomplete)["decision"], "reject")

        malformed = answers()
        malformed["agency_violation"]["noul"] = "unknown"
        self.assertEqual(evaluate_answers(malformed)["decision"], "reject")

    def test_preflight_rejects_missing_authority_before_semantic_gate(self):
        state = {
            "context": "当前场景",
            "branchState": "evidence=[]",
            "playerAction": "观察",
            "resultContract": "保持未知",
            "candidateNarrative": "这里发生了某事。",
            "reviewMeta": {"evidenceSufficient": False},
        }
        preflight = preflight_state(state)

        self.assertFalse(preflight["ok"])
        result = evaluate_answers(answers(), preflight=preflight)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["reasonCodes"], ["insufficient_evidence"])

    def test_authoritative_replay_does_not_turn_observed_anomaly_into_patch(self):
        state = {
            "context": "官方 beat 已明确写出异常现象",
            "branchState": "source=official",
            "playerAction": "复核",
            "resultContract": "只复述证据",
            "candidateNarrative": "异常现象按证据复述。",
            "reviewMeta": {"authoritativeReplay": True},
        }
        preflight = preflight_state(state)
        review = evaluate_answers(answers(values={"bounded_anomaly": 0.9}), preflight=preflight)
        self.assertEqual(review["decision"], "allow")

    def test_authoritative_replay_does_not_bypass_required_fields(self):
        preflight = preflight_state({"reviewMeta": {"authoritativeReplay": True}})
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "missing_required_state")


if __name__ == "__main__":
    unittest.main()
