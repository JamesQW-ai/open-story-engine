import type { CoCreationContext } from "./context-builder.js";
import { directionEvaluationSchema, type DirectionEvaluation } from "./direction-evaluation.js";

export type { DirectionEvaluation } from "./direction-evaluation.js";

export interface DirectionEvaluator {
  evaluate(context: CoCreationContext, playerDirection: string): DirectionEvaluation;
}

const directionSignals: Record<string, string[]> = {
  direction_find_token: ["十七号", "铜牌", "储物柜", "柜子", "线索"],
  direction_secure_evidence: ["站务室", "证据", "录音", "调度"],
  direction_rescue_first: ["救援", "救人", "唐栖", "姜序", "信号室", "隧道"],
  direction_verify_records: ["核实", "记录", "录音", "调度", "失联"],
  direction_enter_tunnel_with_proof: ["隧道", "姜序", "证据", "带着录音"],
  direction_lower_water_with_proof: ["排水", "水位", "手动阀", "阀门"],
  direction_lower_water_without_proof: ["排水", "水位", "手动阀", "阀门"],
  direction_open_signal_room_with_proof: ["打开信号室", "滑栓", "救出", "唐栖"],
  direction_open_signal_room_without_proof: ["打开信号室", "滑栓", "救出", "唐栖"],
  direction_hold_train: ["公开真相", "司机", "列车", "放行", "证据"],
};

const supernaturalSignals = ["魔法", "法术", "超能力", "瞬移", "传送", "复活", "起死回生"];

export class MockDirectionEvaluator implements DirectionEvaluator {
  evaluate(context: CoCreationContext, playerDirection: string): DirectionEvaluation {
    const normalized = normalize(playerDirection);
    if (!normalized) {
      return { kind: "clarification_needed", message: "请用一句话说明希望优先推动哪条剧情方向。" };
    }
    if (normalized.length > 240) {
      return { kind: "clarification_needed", message: "这段方向描述过长。请先用一句话说明当前最想推动的目标。" };
    }

    const noSupernatural = context.availableReferences.find((reference) => reference.kind === "immutable_fact" && reference.ref === "fact_no_supernatural");
    if (noSupernatural && supernaturalSignals.some((signal) => normalized.includes(signal))) {
      return directionEvaluationSchema.parse({
        kind: "rejected",
        message: "当前故事不允许以超自然能力、瞬移或复活直接解决障碍。请在既有世界规则内说明行动。",
        citations: [{ kind: "immutable_fact", ref: noSupernatural.ref }],
      });
    }

    const matches = context.parent.nextDirections.filter((direction) => {
      const signals = directionSignals[direction.id] ?? [direction.title];
      return signals.some((signal) => normalized.includes(normalize(signal)));
    });
    if (matches.length === 1) {
      const direction = matches[0];
      if (direction) {
        return directionEvaluationSchema.parse({
          kind: "accepted",
          directionId: direction.id,
          rationale: `输入与当前方向“${direction.title}”的目标一致。`,
        });
      }
    }
    if (matches.length > 1) {
      return {
        kind: "clarification_needed",
        message: `这段描述同时涉及多条方向：${matches.map((direction) => direction.title).join("、")}。请明确本回合优先哪一条。`,
      };
    }
    return {
      kind: "clarification_needed",
      message: `当前无法将这段描述映射到已公布的剧情方向。可优先考虑：${context.parent.nextDirections.map((direction) => direction.title).join("、")}。`,
    };
  }
}

function normalize(value: string): string {
  return value.trim().replace(/\s+/g, "").replace(/[，。！？、；：“”‘’（）()【】]/g, "").toLowerCase();
}
