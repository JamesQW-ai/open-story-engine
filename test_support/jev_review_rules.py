"""Project-specific Jev review contract used by the independent probe.

This module is deliberately outside the runtime planner. It freezes the local
L0/L1/L2/L3 rule composition without allowing a composite score to override a
hard conflict. The remote model only supplies typed signals; this module owns
the deterministic disposition and the observational layer scores.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


RULE_SET_VERSION = "jev-review-rules/0.3"
FATAL_THRESHOLD = 0.65
DECISIONS = {"allow", "allow_with_patch", "rewrite", "reject"}
FATAL_KEYS = (
    "hard_state_conflict",
    "agency_violation",
    "knowledge_leak",
    "future_fact_leak",
)
BLOCKING_KEYS = FATAL_KEYS
CLAIM_SUPPORT_MIN = 0.35

REASON_CODES = {
    "insufficient_evidence",
    "missing_result_contract",
    "conflicting_authority",
    "missing_required_state",
    "malformed_review_answers",
    "hard_state_conflict",
    "agency_violation",
    "knowledge_leak",
    "future_fact_leak",
    "unsupported_claim",
    "missing_patch_support",
    "model_reject",
    "model_rewrite",
    "bounded_anomaly",
    "allow",
}


QUESTIONS: Dict[str, Dict[str, Any]] = {
    "review_decision": {
        "type": "choice",
        "instructions": (
            "审核 candidateNarrative 相对于 context、branchState、playerAction 和 resultContract 的整体结论。"
            "只依据 state 中的证据判断，不替 StoryPackage 或 BranchState 创造新事实。"
        ),
        "criteria": {
            "allow": "行动、状态、角色知识、剧情节点和世界观均一致，没有需要补救的问题。",
            "allow_with_patch": "只存在范围有限的异常或不确定性；没有状态、玩家授权、角色知识或未来剧情硬冲突；可以用 state 允许的机制、代价、线索和有限期限补救。",
            "rewrite": "存在明确硬事实冲突、玩家越权、角色知识泄露、未来 beat 泄露，或异常缺少机制、代价、线索和解释期限。",
            "reject": "candidateNarrative 缺失、无法理解，或 state 证据不足以进行安全判断。",
        },
    },
    "hard_state_conflict": {
        "type": "noul",
        "instructions": "candidateNarrative 是否明确违反 branchState 中已确认且不可逆的状态或结果？只有明确冲突才回答 true。",
        "criteria": {
            "true": "正文明确恢复已永久损毁的物品、推翻已确认死亡、改变已完成选择或写出其他不可逆状态冲突。",
            "false": "正文保持状态不变，或只提出尚未确认的观察、疑问、仿制品、替身或相似表象。",
        },
    },
    "agency_violation": {
        "type": "noul",
        "instructions": "candidateNarrative 是否替玩家执行了 playerAction 和 resultContract 没有授权的移动、战斗、决定或状态变化？",
        "criteria": {
            "true": "正文替玩家开门、移动、交战、取得物品或完成关键决定，而本回合没有授权。",
            "false": "正文只完成本回合已授权的观察、询问或感知，并保留下一步决定给玩家。",
        },
    },
    "knowledge_leak": {
        "type": "noul",
        "instructions": "candidateNarrative 是否让角色使用没有获知路径的信息，或把玩家私有信息当成 NPC 已知事实？",
        "criteria": {
            "true": "正文明确让 NPC 说出只有玩家知道的秘密，且 state 没有转述、观察或其他获知路径。",
            "false": "角色只使用公开信息、已登记事实或 state 明确提供的获知路径。",
        },
    },
    "future_fact_leak": {
        "type": "noul",
        "instructions": "candidateNarrative 是否泄露 context 当前未公开、且 branchState 标为 future 的地点、人物、事件或答案？",
        "criteria": {
            "true": "正文直接使用 future_locations、future_priest_name 或其他未来 beat 内容，并把它当成当前已知事实。",
            "false": "正文只使用当前已公开上下文，或把未知内容保持为疑问和观察。",
        },
    },
    "claim_supported": {
        "type": "noul",
        "instructions": "candidateNarrative 中会影响状态、因果、角色知识或未来事实的具体断言，是否都能由 allowedEvidence、authoritativeState 或 resultContract 支持？只把这类有影响的断言纳入判断；不改变状态的中性环境、触感或声响描写不应单独触发拒绝。保留疑问、相似表象和来源未知时也视为没有越界。",
        "criteria": {
            "true": "正文只使用允许证据，或明确保持观察、疑问、相似和来源未知；附带的中性氛围描写没有改变状态或因果。",
            "false": "正文添加了会改变状态、因果、角色知识或未来事实的具体人物、地点、物品来源或解释，并以确定语气呈现。",
        },
    },
    "bounded_anomaly": {
        "type": "noul",
        "instructions": "candidateNarrative 是否只呈现一个允许补丁化的异常表象，而没有把未知解释写成已确认事实？",
        "criteria": {
            "true": "正文保留相似、疑似、仿制品、冷风或其他异常的边界，并且 state 的 allowedMechanisms 提供了潜在解释。",
            "false": "正文没有异常，或已经直接确认了未经证实的解释、身份、复生或物品恢复。",
        },
    },
    "missing_patch_support": {
        "type": "noul",
        "instructions": "如果 candidateNarrative 存在异常，它是否缺少 state 要求的机制、代价、可观察线索或有限解释期限，因而不能安全补丁化？没有异常时回答 false。",
        "criteria": {
            "true": "正文把异常直接解释完、没有机制或线索，或补丁会改变已确认历史、状态或玩家选择。",
            "false": "异常仍保持不确定，或已有允许机制并留下了可验证的后续调查方向。",
        },
    },
}


def _probability(answer: Mapping[str, Any] | None) -> float | None:
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("noul")
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _choice(answer: Mapping[str, Any] | None) -> str | None:
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("choice")
    return value if isinstance(value, str) else None


def _observational_scores(answers: Mapping[str, Mapping[str, Any]]) -> Dict[str, int | None]:
    """Return per-layer scores for comparison only; never use them as gates."""

    scores: Dict[str, int | None] = {}
    for key, label in (
        ("hard_state_conflict", "state_consistency"),
        ("agency_violation", "agency_scope"),
        ("knowledge_leak", "knowledge_boundary"),
        ("future_fact_leak", "future_boundary"),
        ("claim_supported", "grounding"),
        ("missing_patch_support", "repair_support"),
    ):
        probability = _probability(answers.get(key))
        if probability is None:
            scores[label] = None
        elif key == "claim_supported":
            scores[label] = round(probability * 100)
        else:
            scores[label] = round((1.0 - probability) * 100)
    available = [value for value in scores.values() if value is not None]
    scores["minimum_observational_score"] = min(available) if available else None
    return scores


def preflight_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Apply deterministic L0 input checks before semantic disposition."""

    required = ("context", "branchState", "playerAction", "resultContract", "candidateNarrative")
    missing = [key for key in required if not isinstance(state.get(key), str) or not state[key].strip()]
    metadata = state.get("reviewMeta")
    if isinstance(metadata, Mapping):
        if metadata.get("evidenceSufficient") is False:
            return {"ok": False, "reason": "insufficient_evidence"}
        if metadata.get("contractPresent") is False:
            return {"ok": False, "reason": "missing_result_contract"}
        if metadata.get("evidenceConflict") is True:
            return {"ok": False, "reason": "conflicting_authority"}
    if missing:
        return {"ok": False, "reason": "missing_required_state", "fields": missing}
    if isinstance(metadata, Mapping) and metadata.get("authoritativeReplay") is True:
        return {"ok": True, "reason": "authoritative_replay", "authoritativeReplay": True}
    return {"ok": True, "reason": "preflight_passed"}


def evaluate_answers(
    answers: Mapping[str, Mapping[str, Any]],
    *,
    preflight: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Compose typed Jev answers into an auditable four-layer review result."""

    direct = _choice(answers.get("review_decision"))
    fatal_risks = {key: _probability(answers.get(key)) for key in FATAL_KEYS}
    blocking_risks = {key: _probability(answers.get(key)) for key in BLOCKING_KEYS}
    claim_support = _probability(answers.get("claim_supported"))
    if claim_support is not None:
        blocking_risks["unsupported_claim"] = 1.0 - claim_support
    fatal = max((value or 0.0 for value in blocking_risks.values()), default=0.0)
    anomaly = _probability(answers.get("bounded_anomaly")) or 0.0
    missing_patch = _probability(answers.get("missing_patch_support")) or 0.0
    authoritative_replay = bool(preflight and preflight.get("authoritativeReplay"))

    missing_required = [key for key in QUESTIONS if key not in answers]
    malformed = [
        key for key in QUESTIONS
        if key != "review_decision" and _probability(answers.get(key)) is None
    ]
    reason_codes: list[str] = []
    if preflight is not None and preflight.get("ok") is False:
        decision = "reject"
        reason = str(preflight.get("reason") or "preflight_failed")
        reason_codes.append(reason if reason in REASON_CODES else "missing_required_state")
    elif direct not in DECISIONS or missing_required or malformed:
        decision = "reject"
        reason = "typed_answers_incomplete_or_invalid"
        reason_codes.append("malformed_review_answers")
    elif fatal >= FATAL_THRESHOLD or missing_patch >= FATAL_THRESHOLD or (
        claim_support is not None and claim_support < CLAIM_SUPPORT_MIN
    ):
        decision = "rewrite"
        reason = "fatal_or_unsupported_patch_risk"
        if missing_patch >= FATAL_THRESHOLD:
            reason_codes.append("missing_patch_support")
        if claim_support is not None and claim_support < CLAIM_SUPPORT_MIN:
            reason_codes.append("unsupported_claim")
        for key in FATAL_KEYS:
            if (fatal_risks.get(key) or 0.0) >= FATAL_THRESHOLD:
                reason_codes.append(key)
    elif direct == "reject":
        decision = "reject"
        reason = "model_reject"
        reason_codes.append("model_reject")
    elif direct == "rewrite":
        decision = "rewrite"
        reason = "model_rewrite"
        reason_codes.append("model_rewrite")
    elif direct == "allow_with_patch" or (anomaly >= FATAL_THRESHOLD and not authoritative_replay):
        decision = "allow_with_patch"
        reason = "bounded_anomaly_or_model_patch"
        reason_codes.append("bounded_anomaly")
    elif direct == "allow":
        decision = "allow"
        reason = "no_blocking_signal"
        reason_codes.append("allow")
    else:  # Defensive branch for future rule-set edits.
        decision = "reject"
        reason = "unhandled_decision"

    return {
        "ruleSetVersion": RULE_SET_VERSION,
        "decision": decision,
        "reason": reason,
        "reasonCodes": reason_codes or ["malformed_review_answers"],
        "layers": {
            "L0": {
                "status": "rewrite" if fatal >= FATAL_THRESHOLD else "pass",
                "fatalRisks": fatal_risks,
                "blockingRisks": blocking_risks,
                "threshold": FATAL_THRESHOLD,
            },
            "L1": {
                "directDecision": direct,
                "boundedAnomaly": anomaly,
                "missingPatchSupport": missing_patch,
            },
            "L2": {
                "decision": decision,
                "codeOwned": True,
            },
            "L3": {
                "scores": _observational_scores(answers),
                "scoreIsObservational": True,
            },
        },
    }
