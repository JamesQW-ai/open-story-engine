"""Deterministic context-budget enforcement for ContextBundle snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .context_bundle import ContextBundle, ContextBundleError, validate_bundle


class ContextBudgetError(ContextBundleError):
    """Raised when the minimum protected context cannot fit the input budget."""

    def __init__(self, message: str, *, protected_tokens: int, available_tokens: int) -> None:
        super().__init__(message)
        self.code = "context_budget_exceeded"
        self.protected_tokens = protected_tokens
        self.available_tokens = available_tokens


TokenEstimator = Callable[[str], int]


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens without treating CJK characters as four-token bytes.

    This remains an estimate and provider usage is authoritative.  Wide East
    Asian characters are counted individually; other text keeps the portable
    four-characters-per-token approximation.
    """
    wide = sum(1 for char in text if unicodedata.east_asian_width(char) in {"W", "F"})
    narrow = len(text) - wide
    return max(1, wide + math.ceil(narrow / 4))


@dataclass(frozen=True)
class ContextBudgetResult:
    bundle: ContextBundle
    available_tokens: int
    protected_tokens: int
    selected_tokens: int
    omitted_sources: tuple[str, ...]
    estimated: bool


def _tokens(value: Any, estimator: TokenEstimator) -> int:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return estimator(text)


def _is_protected(item: Mapping[str, Any]) -> bool:
    tier = item.get("tier")
    if tier == "protected":
        return True
    if item.get("authority") == "authoritative":
        return True
    # Evidence authority records whether a source is trusted; it does not
    # decide whether the source is indispensable for every stage.  Current
    # beat and confirmed public events/facts are hard inputs.  Recent prose,
    # dialogue and other confirmed evidence remain budgetable soft context.
    return item.get("kind") in {"current_beat", "public_fact", "confirmed_event"}


def _drop_key(item: Mapping[str, Any]) -> tuple:
    # Lower values are dropped first.  This keeps the current beat and recent
    # confirmed evidence while removing style and older soft continuity first.
    kind_order = {
        "style_sample": 0,
        "recent_prose": 1,
        "continuity_summary": 1,
        "soft_evidence": 2,
    }
    return (kind_order.get(str(item.get("kind")), 2), str(item.get("sourceId", "")))


def apply_context_budget(
    bundle: ContextBundle,
    *,
    context_window_tokens: int,
    reserved_output_tokens: int = 0,
    estimator: Optional[TokenEstimator] = None,
    stage: str = "chapter",
) -> ContextBudgetResult:
    """Keep protected evidence and deterministically omit soft evidence.

    This first implementation never asks a model to summarize context.  That
    keeps the result auditable while the bundle contract is being integrated.
    """
    validate_bundle(bundle)
    if context_window_tokens <= 0:
        raise ContextBudgetError("context window 必须大于零。", protected_tokens=0, available_tokens=0)
    if reserved_output_tokens < 0:
        raise ContextBudgetError(
            "reserved output tokens 不能为负数。",
            protected_tokens=0,
            available_tokens=context_window_tokens,
        )
    estimate = estimator or estimate_text_tokens
    available = context_window_tokens - reserved_output_tokens
    projection = bundle.project(stage)
    base_projection = {
        key: value for key, value in projection.items()
        if key not in {"allowedEvidence", "continuityWindow", "styleGuide", "dynamicMemory"}
    }
    stage_evidence = list(projection.get("allowedEvidence", []))
    protected_items = [item for item in stage_evidence if _is_protected(item)]
    soft_items = [item for item in stage_evidence if not _is_protected(item)]
    protected_payload = {
        **base_projection,
        "allowedEvidence": protected_items,
    }
    # Confirmed dynamic memory has a higher priority than short-term
    # continuity and must never disappear during budget reduction. Use the
    # stage projection so hidden author truth is not charged to player prose.
    if "dynamicMemory" in projection:
        protected_payload["dynamicMemory"] = list(projection["dynamicMemory"])
    protected_tokens = _tokens(protected_payload, estimate)
    if protected_tokens > available:
        raise ContextBudgetError(
            f"硬上下文超出输入预算 ({protected_tokens}/{available})。",
            protected_tokens=protected_tokens,
            available_tokens=available,
        )

    selected = list(soft_items)
    selected_continuity = list(projection.get("continuityWindow", []))
    selected_style = dict(projection.get("styleGuide", {}))

    def payload() -> Dict[str, Any]:
        result = {
            **protected_payload,
            "allowedEvidence": protected_items + selected,
        }
        if "continuityWindow" in projection:
            result["continuityWindow"] = selected_continuity
        if "styleGuide" in projection:
            result["styleGuide"] = selected_style
        return result

    selected_payload = payload()
    selected_tokens = _tokens(selected_payload, estimate)
    omitted: List[str] = []
    if selected_tokens > available and selected_style:
        selected_style = {}
        omitted.append("styleGuide")
        selected_tokens = _tokens(payload(), estimate)
    while selected_tokens > available and selected_continuity:
        item = selected_continuity.pop(0)
        omitted.append("continuity:" + str(item.get("sourceId", len(omitted))))
        selected_tokens = _tokens(payload(), estimate)
    for item in sorted(soft_items, key=_drop_key):
        if selected_tokens <= available:
            break
        source_id = str(item.get("sourceId"))
        selected = [candidate for candidate in selected if candidate.get("sourceId") != source_id]
        omitted.append(source_id)
        selected_tokens = _tokens(payload(), estimate)

    if selected_tokens > available:
        raise ContextBudgetError(
            f"最小有效上下文超出输入预算 ({selected_tokens}/{available})。",
            protected_tokens=protected_tokens,
            available_tokens=available,
        )
    if not omitted:
        return ContextBudgetResult(bundle, available, protected_tokens, selected_tokens, (), estimator is None)

    provenance = dict(bundle.provenance)
    existing = list(provenance.get("excludedSources", [])) if isinstance(provenance.get("excludedSources"), list) else []
    provenance["excludedSources"] = sorted(
        set(existing + [item for item in omitted if item != "styleGuide" and not item.startswith("continuity:")])
    )
    budget = dict(bundle.budget)
    budget.update({
        "availableTokens": available,
        "selectedTokens": selected_tokens,
        "protectedTokens": protected_tokens,
        "estimated": estimator is None,
        "omittedSoftItems": omitted,
    })
    omitted_ids = {
        item for item in omitted
        if item != "styleGuide" and not item.startswith("continuity:")
    }
    reduced_allowed_evidence = [
        item for item in bundle.allowed_evidence
        if item.get("sourceId") not in omitted_ids
    ]
    reduced = ContextBundle.create(
        context_id=bundle.context_id,
        package=bundle.package,
        branch=bundle.branch,
        hard_constraints=bundle.hard_constraints,
        turn_intent=bundle.turn_intent,
        authoritative_state=bundle.authoritative_state,
        state_visibility=bundle.state_visibility,
        allowed_evidence=reduced_allowed_evidence,
        continuity_window=selected_continuity,
        style_guide=selected_style,
        output_contract=bundle.output_contract,
        dynamic_memory=bundle.dynamic_memory,
        provenance=provenance,
        budget=budget,
        parent_context_id=bundle.parent_context_id,
    )
    return ContextBudgetResult(reduced, available, protected_tokens, selected_tokens, tuple(omitted), estimator is None)
