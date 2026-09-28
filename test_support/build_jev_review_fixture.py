"""Build the broader, deterministic Jev review fixture from the base cases."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "test_support" / "fixtures" / "jev-narrative-review.json"
DEFAULT_OUTPUT = ROOT / "test_support" / "fixtures" / "jev-narrative-review-expanded.json"


EXTRA_CASES = [
    {
        "id": "pass_long_context_noise",
        "expectedDecision": "allow",
        "variant": "long_context_noise",
        "context": "当前场景是登记堂。已确认木牌在玩家手中，陆照临在场且知道公开的白线规则，玩家只说明准备参加后坡试炼。\n" + "无关旧记录：" * 90,
        "branchState": "player_has_token=true; steward_alive=true; location=registration_hall; current_beat=trial_intent; unrelated_branch_secret=hidden",
        "playerAction": "我说明准备参加后坡试炼，暂不出发。",
        "resultContract": "只陈述打算并等待回应；不得移动、战斗或揭示其他分支秘密。",
        "allowedMechanisms": [],
        "candidateNarrative": "你说明准备带着木牌参加后坡试炼，随后停在原地等陆照临回应。他只提醒你遵守当前已知的白线规则，没有替你决定下一步。",
    },
    {
        "id": "pass_multi_character_scope",
        "expectedDecision": "allow",
        "variant": "multi_character_scope",
        "context": "玩家、陆照临和沈砚秋都在登记堂；玩家只询问木牌登记依据。陆照临知道公开规则，沈砚秋负责登记。",
        "branchState": "location=registration_hall; player_has_token=true; lu_knows_public_rules=true; shen_is_registrar=true",
        "playerAction": "我询问木牌登记依据。",
        "resultContract": "只允许两名 NPC 依据公开职责回应；玩家不移动、不交出木牌。",
        "allowedMechanisms": [],
        "candidateNarrative": "沈砚秋指着登记册说明木牌需要核验，陆照临只补充他听过的公开规矩。你仍把木牌留在手里，没有作出下一步决定。",
    },
    {
        "id": "pass_observation_stays_uncertain",
        "expectedDecision": "allow_with_patch",
        "variant": "uncertain_observation",
        "context": "门后的声音来源未知；没有确认有人、机关或暗道。玩家只选择听和观察。",
        "branchState": "door_closed=true; sound_source=unknown; hidden_mechanism_unconfirmed=true",
        "playerAction": "我贴近门边听声音。",
        "resultContract": "只描述声音和可观察反应，不确认来源，不开门。",
        "allowedMechanisms": ["hidden_vent", "distant_step"],
        "candidateNarrative": "门后传来两下轻响，间隔并不规律。你没有判断那是脚步还是机关，只记下声音的位置，仍未伸手开门。",
    },
    {
        "id": "pass_style_noise_only",
        "expectedDecision": "allow",
        "variant": "soft_style_noise",
        "context": "玩家已获得公开通行凭证，值守者在登记堂门口，当前只需要确认登记堂是否开放。没有状态冲突或隐藏事实。",
        "branchState": "pass_granted=true; registration_hall_open=true; player_location=outer_hall",
        "playerAction": "我询问登记堂是否开放。",
        "resultContract": "完成一次询问即可；不要移动或新增世界事实。",
        "allowedMechanisms": [],
        "candidateNarrative": "你再次确认登记堂是否开放，值守者点头回答已经开放。你听完答复，仍留在原地，没有新增行动。",
    },
    {
        "id": "repairable_replica_key",
        "expectedDecision": "allow_with_patch",
        "variant": "repairable_replica",
        "context": "唯一的原钥匙已在账本中确认折断；世界观允许复制钥匙，但当前只看到一把外观相似的钥匙。",
        "branchState": "original_key_broken=true; original_key_repairable=false; copy_key_allowed=true; copy_origin=unknown",
        "playerAction": "我检查桌上的钥匙。",
        "resultContract": "只能记录外观相似和来源未知，不得称其为原钥匙或使用它开门。",
        "allowedMechanisms": ["copy_key", "decoy_key"],
        "candidateNarrative": "桌上的钥匙轮廓与折断的原钥匙相似，却没有确认它就是原物。你先记录齿口的差异，决定之后查清它的来源。",
        "patchHint": "后续需要给出复制钥匙的来源或可观察的制作线索。",
    },
    {
        "id": "repairable_unknown_sound",
        "expectedDecision": "allow_with_patch",
        "variant": "repairable_environment",
        "context": "石墙后有持续回声，来源未知；世界观允许暗槽，但尚未确认墙后存在房间。",
        "branchState": "echo_observed=true; wall_room_confirmed=false; hidden_slot_allowed=true",
        "playerAction": "我观察石墙上的缝隙。",
        "resultContract": "可以描述回声和缝隙，但不得宣布找到暗槽或墙后房间。",
        "allowedMechanisms": ["hidden_slot", "water_channel"],
        "candidateNarrative": "石墙缝里传出闷闷的回声，像是从更深处折回来。你只确认缝隙存在，没有判断墙后是什么，随后记下了需要检查的位置。",
        "patchHint": "后续通过敲击或取样确认回声来源，并承担暴露位置的风险。",
    },
    {
        "id": "repairable_identity_glimpse",
        "expectedDecision": "allow_with_patch",
        "variant": "repairable_identity",
        "context": "失踪的抄录员只被记录为疑似死亡，没有遗体；雨幕遮挡身份；世界观允许替身，但未确认替身出现。",
        "branchState": "copyist_status=suspected_dead; body_confirmed=false; visibility=poor; identity_unresolved=true",
        "playerAction": "我远远观察廊下的人影。",
        "resultContract": "只能描述相似动作和不确定性，不确认身份或状态改变。",
        "allowedMechanisms": ["decoy", "false_death", "mistaken_identity"],
        "candidateNarrative": "廊下的人影抬手的方式像那名抄录员，但雨幕遮住了脸。你只能记住这个相似处，不能确认他已经活着回来。",
        "patchHint": "后续必须提供身份核验线索，并解释此前为何没有被发现。",
    },
    {
        "id": "repairable_cold_draft_only",
        "expectedDecision": "allow_with_patch",
        "variant": "repairable_environment_refined",
        "context": "封闭登记堂内确认有冷风贴近地面，但来源未知；没有确认门外下雨、有人或暗道。",
        "branchState": "outer_door_closed=true; cold_draft_observed=true; source_of_draft=unknown",
        "playerAction": "我观察门缝和地面。",
        "resultContract": "只描述冷风、门缝和地面；不得新增门外天气、人物或已确认的来源。",
        "allowedMechanisms": ["hidden_vent", "unseen_gap"],
        "candidateNarrative": "冷风贴着你的鞋面掠过，门缝边积着细灰。你没有判断风从哪里来，只记下了这处异常，准备随后检查墙面和门缝。",
        "patchHint": "后续用可观察线索确认冷风来源，不得直接宣布找到暗道。",
    },
    {
        "id": "rewrite_unsupported_environment_claim",
        "expectedDecision": "rewrite",
        "variant": "unsupported_environment_claim",
        "context": "封闭登记堂内确认有冷风贴近地面，但来源未知；没有确认门外下雨、有人或暗道。",
        "branchState": "outer_door_closed=true; cold_draft_observed=true; source_of_draft=unknown",
        "playerAction": "我观察门缝和地面。",
        "resultContract": "只描述冷风、门缝和地面；不得新增门外天气、人物或已确认的来源。",
        "allowedMechanisms": ["hidden_vent", "unseen_gap"],
        "candidateNarrative": "门外的雨声从门缝里压进来，说明墙后一定连着一条通向暗河的通道。",
        "patchHint": "缺少支持门外下雨和暗河的证据，必须保持未知或重写。",
    },
    {
        "id": "fatal_guardian_revived",
        "expectedDecision": "rewrite",
        "variant": "hard_state_conflict",
        "context": "守门人已确认死亡，遗体与记录均已核实；世界观没有复生机制。",
        "branchState": "guardian_status=confirmed_dead; body_confirmed=true; resurrection_allowed=false",
        "playerAction": "我观察守门处。",
        "resultContract": "只能描述空岗、遗留物或其他人物，不得让守门人正常出现。",
        "allowedMechanisms": [],
        "candidateNarrative": "守门人从阴影里走出来，像什么都没发生过一样向你挥手。",
    },
    {
        "id": "fatal_consumed_artifact",
        "expectedDecision": "rewrite",
        "variant": "hard_state_conflict",
        "context": "唯一的引路灯已在上一节点被消耗并从状态账本移除；不可复制、不可恢复。",
        "branchState": "guide_lamp_consumed=true; guide_lamp_exists=false; duplicate_allowed=false",
        "playerAction": "我查看祭台残留。",
        "resultContract": "只能描述灰烬和余温，不得让引路灯重新点亮。",
        "allowedMechanisms": [],
        "candidateNarrative": "祭台中央重新亮起那盏已经消耗掉的引路灯，火焰照亮了整座大厅。",
    },
    {
        "id": "fatal_branch_jump",
        "expectedDecision": "rewrite",
        "variant": "future_branch_leak",
        "context": "当前分支只公开登记堂和后坡试炼；兄弟分支才揭示黑塔入口，玩家尚未进入该分支。",
        "branchState": "known_locations=[registration_hall,back_slope]; sibling_branch_locations=[black_tower]; sibling_branch_access=false",
        "playerAction": "我询问后坡试炼的规则。",
        "resultContract": "只能回应当前分支已公开的试炼规则，不得提及兄弟分支地点。",
        "allowedMechanisms": [],
        "candidateNarrative": "值守者告诉你，真正的答案在兄弟分支的黑塔入口，那里已经有人替你安排好了一切。",
    },
    {
        "id": "fatal_location_teleport",
        "expectedDecision": "rewrite",
        "variant": "state_conflict",
        "context": "玩家仍在登记堂，铁门关闭；本回合只选择查看门上的刻痕。",
        "branchState": "location=registration_hall; iron_gate=closed; transition_authorized=false",
        "playerAction": "我查看铁门上的刻痕。",
        "resultContract": "停留在登记堂观察，不转场。",
        "allowedMechanisms": [],
        "candidateNarrative": "你看完刻痕后已经站在白塔祭坛前，身后的登记堂和铁门都消失了。",
    },
    {
        "id": "fatal_unrequested_attack",
        "expectedDecision": "rewrite",
        "variant": "agency_violation",
        "context": "玩家只选择询问守卫，守卫没有攻击；当前没有授权战斗。",
        "branchState": "guard_present=true; guard_hostile=false; player_action_scope=ask_only",
        "playerAction": "我询问守卫登记规则。",
        "resultContract": "只进行询问和回应，不拔武器、不伤害守卫。",
        "allowedMechanisms": [],
        "candidateNarrative": "你拔刀刺向守卫，在他倒下后夺走了门上的徽记。",
    },
    {
        "id": "fatal_unrequested_item",
        "expectedDecision": "rewrite",
        "variant": "agency_violation",
        "context": "玩家只选择观察柜台，柜台后的药瓶尚未被允许取得。",
        "branchState": "counter_observed=true; elixir_available=true; item_take_authorized=false",
        "playerAction": "我观察柜台。",
        "resultContract": "描述药瓶和柜台即可，不拿取或饮用药瓶。",
        "allowedMechanisms": [],
        "candidateNarrative": "你伸手拿起药瓶一饮而尽，身体立刻恢复了力气。",
    },
    {
        "id": "fatal_unrequested_exit",
        "expectedDecision": "rewrite",
        "variant": "agency_violation",
        "context": "玩家选择等待回应，角色仍站在原地；本回合没有移动授权。",
        "branchState": "location=archive_hall; movement_authorized=false; response_pending=true",
        "playerAction": "我等着对方回应。",
        "resultContract": "保持原地等待，不离开场景、不完成目标。",
        "allowedMechanisms": [],
        "candidateNarrative": "你转身离开登记堂，穿过长廊并完成了入门登记。",
    },
    {
        "id": "fatal_private_code",
        "expectedDecision": "rewrite",
        "variant": "knowledge_leak",
        "context": "只有玩家从密信中看到‘东井’暗号，NPC 没有观察或转述路径。",
        "branchState": "secret_known_by=[player]; npc_knowledge_path=[]; relay_event=false",
        "playerAction": "我把密信收好，不解释内容。",
        "resultContract": "NPC 不得直接说出东井暗号。",
        "allowedMechanisms": [],
        "candidateNarrative": "守卫立即说出东井暗号，并问你是否已经找到入口。",
    },
    {
        "id": "fatal_future_person",
        "expectedDecision": "rewrite",
        "variant": "future_fact_leak",
        "context": "祭司姓名只会在后续 beat 揭示；当前公开人物只有值守者和陆照临。",
        "branchState": "known_characters=[guard,lu]; future_priest_name=unrevealed; future_reveal_allowed=false",
        "playerAction": "我询问登记堂的负责者。",
        "resultContract": "只能回应当前公开身份，不提前给出祭司姓名。",
        "allowedMechanisms": [],
        "candidateNarrative": "值守者说祭司顾长庚马上会来替你处理这件事。",
    },
    {
        "id": "fatal_other_branch_secret",
        "expectedDecision": "rewrite",
        "variant": "sibling_branch_leak",
        "context": "另一个分支才公开旧王印的来源；当前分支只知道它是一件未登记物品。",
        "branchState": "item_origin=unknown; sibling_branch_origin=old_king_seal; sibling_branch_visible=false",
        "playerAction": "我观察手中的印记。",
        "resultContract": "只描述外观和未知来源，不揭示兄弟分支答案。",
        "allowedMechanisms": [],
        "candidateNarrative": "你立刻想起旧王印的来源，并确认它来自被封存的王室宝库。",
    },
    {
        "id": "reject_empty_evidence",
        "expectedDecision": "reject",
        "variant": "insufficient_evidence",
        "context": "当前没有可用的场景事实或角色登记信息。",
        "branchState": "evidence=[]; state_snapshot=missing",
        "playerAction": "我询问眼前发生了什么。",
        "resultContract": "证据不足时不得把任何解释写成事实。",
        "allowedMechanisms": [],
        "candidateNarrative": "这里显然是旧王室的密室，守门人已经在等你。",
        "reviewMeta": {"evidenceSufficient": False},
    },
    {
        "id": "reject_conflicting_state",
        "expectedDecision": "reject",
        "variant": "conflicting_evidence",
        "context": "一份未核实摘要说门已打开，但权威状态和现场证据都没有确认门的状态。",
        "branchState": "authoritative_gate_state=unknown; summary_gate_state=open; evidence_conflict=true",
        "playerAction": "我决定根据眼前情况继续。",
        "resultContract": "冲突未解决前不得断言门开或门关。",
        "allowedMechanisms": [],
        "candidateNarrative": "铁门已经打开，你毫不犹豫地走了进去。",
        "reviewMeta": {"evidenceConflict": True},
    },
    {
        "id": "reject_missing_contract",
        "expectedDecision": "reject",
        "variant": "missing_output_contract",
        "context": "当前场景和状态可以读取，但本回合没有定义允许的行动范围或停止点。",
        "branchState": "location=registration_hall; player_has_token=true; output_contract=missing",
        "playerAction": "我继续处理眼前的事。",
        "resultContract": "",
        "allowedMechanisms": [],
        "candidateNarrative": "你打开铁门，走到另一座大厅并决定接受新的任务。",
        "reviewMeta": {"contractPresent": False},
    },
]


def build() -> dict:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    cases = copy.deepcopy(base["cases"])
    for case in cases:
        if case["id"] == "environment_small_hallucination":
            case["expectedDecision"] = "rewrite"
            case["labelRevision"] = "证据只确认冷风，原候选新增门外雨声，按 unsupported_claim 归为重写。"
        if case["id"] == "pass_consistent":
            case["candidateNarrative"] = "你没有移动铜牌，只抬眼问沈执事登记堂的入口。沈执事指向前方，说那里仍有人值守。"
    existing = {case["id"] for case in cases}
    for case in EXTRA_CASES:
        if case["id"] in existing:
            raise ValueError(f"duplicate case id: {case['id']}")
        cases.append(case)
    return {
        "schemaVersion": "jev-narrative-review-fixture/0.2",
        "sourceFixture": "jev-narrative-review.json",
        "description": "扩展的 Jev 剧情审核矩阵；用于独立探针，不计入正式产品质量验收。",
        "caseCount": len(cases),
        "coverage": {
            "baseCases": len(base["cases"]),
            "expandedCases": len(EXTRA_CASES),
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
