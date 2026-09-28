"""Build a long-context Jev stress fixture from reviewed cases and real drafts."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPANDED = ROOT / "test_support" / "fixtures" / "jev-narrative-review-expanded.json"
REAL = ROOT / "test_support" / "fixtures" / "jev-real-longform-review.json"
DEFAULT_OUTPUT = ROOT / "test_support" / "fixtures" / "jev-narrative-review-longform.json"


LONG_CONTEXT_NOTES = [
    "当前包为 taixu-relics-part1@0.1.3；本段只读取当前分支，不把其他分支的结论写入当前事实。",
    "当前场景的可见实体、位置、持有物和停止点由 branchState 与 resultContract 共同限定。",
    "历史摘要可能包含已解决、未证实或只在兄弟分支出现的内容；归档摘要不具有当前权威性。",
    "角色知识必须有当回合的观察、对话、登记或公开规则路径；玩家知道的内容不会自动传给 NPC。",
    "未来 beat、未登记人物、未确认地点、未核实物品来源只能作为疑问或观察，不能写成确定事实。",
    "可补丁异常必须保留不确定性，并留下机制、代价、线索或有限期限；补丁不能改写已经确认的状态。",
    "玩家只授权本回合写明的动作。等待、询问、观察和陈述打算不等于移动、战斗、取物或完成目标。",
    "短期风声、触感、光线和背景交谈属于中性描写，除非正文把它们升级为状态、因果或身份结论。",
    "正式提交前仍需由本地代码检查包版本、分支、状态投影、事实登记和结果契约。",
    "这份长上下文只用于压力测试输入边界，不能替代当前 state 中明确标记的 authoritative evidence。",
]


def _long_context(case: dict, index: int) -> str:
    notes = []
    for round_no in range(3):
        for note_no, note in enumerate(LONG_CONTEXT_NOTES):
            notes.append(f"归档检索片段 {round_no + 1:02d}-{note_no + 1:02d}：{note}")
    notes.append(
        "当前案例标识为 "
        + case["id"]
        + f"；这是第 {index:02d} 个长上下文变体。以上归档材料只提供检索噪声，不能覆盖案例正文中的当前事实。"
    )
    return case["context"] + "\n\n【长上下文检索结果】\n" + "\n".join(notes)


def _long_branch_state(case: dict, index: int) -> str:
    return (
        case["branchState"]
        + "; context_window=longform_stress"
        + f"; archived_note_count={len(LONG_CONTEXT_NOTES) * 3 + 1}"
        + f"; variant_index={index}"
        + "; authoritative_source=case_state"
    )


def _build_long_variant(case: dict, index: int) -> dict:
    variant = copy.deepcopy(case)
    variant["id"] = f"longctx_{case['id']}"
    variant["variant"] = "long_context_retrieval_noise"
    variant["sourceCaseId"] = case["id"]
    variant["context"] = _long_context(case, index)
    variant["branchState"] = _long_branch_state(case, index)
    variant["labelNote"] = "沿用已复核案例标签，仅增加长上下文检索噪声。"
    return variant


LONG_CANDIDATE_SUFFIXES = {
    "taixu_inform_rerun2": (
        "你没有把这句话说成已经出发，也没有把木牌或纸包递给陆照临。"
        "他只依据公开的白线规则回应，说明日落前取回青露草、不能越界、不能争抢伤人。"
        "你把这些限制逐条记在心里，仍站在原来的位置，确认自己只是说明打算而不是执行行动。"
        "石壁那边没有新的召唤，后坡的路线也没有在这一刻自动展开。"
        "陆照临没有替你决定先后，也没有把金屑的来源说成自己知道的事实。"
        "你仍可以等钟响、继续询问，或者在下一回合再决定是否出发；这一回合到此停住。"
    ),
    "taixu_ask_basis_rerun1": (
        "你把问题重新说了一遍，确认自己问的是依据，而不是要求他替你判断金屑的来历。"
        "陆照临仍只看见木牌、黑泥和你手中的纸包，没有伸手，也没有把玩家的观察当成自己的记忆。"
        "他能引用的只有已经公开的登记规则，至于刻痕里夹着什么、金屑从哪里来，他明确表示没有亲眼见过。"
        "你没有把纸包交给他，也没有把这句询问写成已经得到答案。"
        "两个人都留在原处，木牌仍在你的掌心，后坡试炼尚未开始，钟声也没有提前响起。"
        "接下来是否继续查问、是否要求核验，仍然留给玩家在下一步选择。"
    ),
    "taixu_brief_wait_rerun3": (
        "你又检查了一遍折口和木牌边缘，确认纸包没有松开，木牌也没有从手中滑落。"
        "石壁上的旧剑痕只停留在余光里，场边的人声时断时续，没有人因此向你发出新的要求。"
        "你没有根据这些声响判断有人靠近，也没有把冷风、光线或触感解释成新的机关。"
        "陆照临仍在原处，既没有主动替你推进试炼，也没有把尚未发生的回应写成已经发生。"
        "钟声尚未响起，等待条件没有改变；你只是完成收好物品和停在原地这两个动作。"
        "这一段保持在可观察的现在时，后续是否继续等待，仍由玩家决定。"
    ),
}


def _build_long_candidate_variant(case: dict, index: int) -> dict:
    variant = _build_long_variant(case, index)
    variant["id"] = f"real_longcandidate_{case['id']}"
    variant["variant"] = "official_longform_long_candidate"
    variant["candidateNarrative"] = case["candidateNarrative"] + "\n\n" + LONG_CANDIDATE_SUFFIXES[case["id"]]
    variant["labelNote"] = "复用官方长篇真实候选正文，并增加有证据的连续细节；不改变当前状态或停止点。"
    return variant


def build() -> dict:
    expanded = json.loads(EXPANDED.read_text(encoding="utf-8"))
    real = json.loads(REAL.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in expanded["cases"]}
    real_by_id = {case["id"]: case for case in real["cases"]}
    long_case_ids = [
        "pass_consistent",
        "pass_long_context_noise",
        "pass_multi_character_scope",
        "pass_style_noise_only",
        "false_death_ambiguous",
        "repairable_replica_key",
        "repairable_identity_glimpse",
        "repairable_cold_draft_only",
        "explicit_death_conflict",
        "unauthorized_agency",
        "knowledge_leak",
        "future_beat_leak",
        "rewrite_unsupported_environment_claim",
        "fatal_location_teleport",
        "reject_empty_evidence",
        "reject_conflicting_state",
        "reject_missing_contract",
    ]
    long_variants = [_build_long_variant(by_id[case_id], index) for index, case_id in enumerate(long_case_ids, 1)]
    real_cases = []
    for case in real["cases"]:
        variant = copy.deepcopy(case)
        variant["id"] = f"real_longctx_{case['id']}"
        variant["variant"] = "official_longform_long_context"
        variant["sourceCaseId"] = case["id"]
        variant["context"] = _long_context(case, len(long_variants) + len(real_cases) + 1)
        variant["branchState"] = _long_branch_state(case, len(long_variants) + len(real_cases) + 1)
        variant["labelNote"] = "复用《太虚遗录》官方长篇真实候选正文，增加长上下文检索噪声。"
        real_cases.append(variant)

    long_candidate_cases = [
        _build_long_candidate_variant(real_by_id[case_id], len(long_variants) + len(real_cases) + index)
        for index, case_id in enumerate(
            ("taixu_inform_rerun2", "taixu_ask_basis_rerun1", "taixu_brief_wait_rerun3"),
            1,
        )
    ]

    cases = copy.deepcopy(expanded["cases"]) + long_variants + real_cases + long_candidate_cases
    return {
        "schemaVersion": "jev-narrative-review-fixture/0.3",
        "sourceFixtures": [
            "jev-narrative-review-expanded.json",
            "jev-real-longform-review.json",
        ],
        "description": "Jev 长上下文与长篇真实候选压力夹具；用于独立探针，不计入正式产品质量验收。",
        "caseCount": len(cases),
        "coverage": {
            "expandedCases": len(expanded["cases"]),
            "longContextVariants": len(long_variants),
            "realLongformVariants": len(real_cases),
            "longCandidateVariants": len(long_candidate_cases),
            "longContextNoteCount": len(LONG_CONTEXT_NOTES) * 3 + 1,
            "variants": sorted({case.get("variant", "base") for case in cases}),
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "caseCount": payload["caseCount"], "coverage": payload["coverage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
