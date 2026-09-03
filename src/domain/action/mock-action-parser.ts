import type { StoryPackage } from "../../content/story-package.js";
import type { ActionIntent } from "../rules/rule-engine.js";

export type ActionParseResult =
  | { kind: "parsed"; intent: ActionIntent }
  | { kind: "clarification"; message: string };

type Candidate = {
  actionType: ActionIntent["actionType"];
  targetId: string;
  targetTerms: string[];
};

const actionTerms: Record<ActionIntent["actionType"], string[]> = {
  investigate: ["寻找", "找", "查", "检查", "调查", "查看", "翻", "观察", "搜"],
  negotiate: ["询问", "问", "交谈", "沟通", "请求", "拜托", "劝", "说服", "告诉", "说明"],
  risk: ["进入", "进去", "冲", "闯", "绕过", "冒险", "打开", "撬", "拉", "拧", "涉水", "救"],
};

const targetTerms: Record<string, string[]> = {
  item_locker_token: ["储物柜", "十七号柜", "铜牌", "柜牌"],
  location_station_office: ["站务室", "消防通道", "办公室"],
  item_recorder: ["录音笔", "录音", "调度记录", "档案"],
  character_jiang_xu: ["姜序", "维修工"],
  item_relief_valve: ["手动阀", "排水阀", "阀门", "水位"],
  item_signal_door: ["信号室", "滑栓", "信号门"],
  character_train_driver: ["列车司机", "司机"],
};

export class MockActionParser {
  private readonly candidates: Candidate[];

  constructor(storyPackage: StoryPackage) {
    const entityEntries: Array<readonly [string, string]> = [
      ...storyPackage.items.map((item) => [item.id, item.name] as const),
      ...storyPackage.locations.map((location) => [location.id, location.name] as const),
      ...storyPackage.characters.map((character) => [character.id, character.name] as const),
    ];
    const entityNames = new Map<string, string>(entityEntries);
    const candidates = new Map<string, Candidate>();
    storyPackage.rules.resolutions.forEach((resolution) => {
      const key = `${resolution.actionType}:${resolution.targetId}`;
      candidates.set(key, {
        actionType: resolution.actionType,
        targetId: resolution.targetId,
        targetTerms: [entityNames.get(resolution.targetId) ?? resolution.targetId, ...(targetTerms[resolution.targetId] ?? [])],
      });
    });
    this.candidates = [...candidates.values()];
  }

  parse(input: string): ActionParseResult {
    const normalized = input.trim();
    if (!normalized) return { kind: "clarification", message: "许川还没有采取行动。请用一句话说明他想做什么。" };
    if (normalized.length > 160) return { kind: "clarification", message: "这段行动描述过长。请保留眼下最想尝试的一件事。" };

    const matches = this.candidates
      .map((candidate) => ({ candidate, score: scoreCandidate(normalized, candidate) }))
      .filter((entry) => entry.score > 0)
      .sort((left, right) => right.score - left.score);
    const best = matches[0];
    if (!best || (matches[1] && matches[1].score === best.score)) {
      return { kind: "clarification", message: "许川能听见雨声和站内的动静，但还无法判断他要把注意力放在哪里。请把对象说得更明确一些。" };
    }

    return {
      kind: "parsed",
      intent: { actionType: best.candidate.actionType, targetId: best.candidate.targetId, approach: normalized },
    };
  }
}

function scoreCandidate(input: string, candidate: Candidate): number {
  const targetScore = candidate.targetTerms.some((term) => input.includes(term)) ? 10 : 0;
  if (targetScore === 0) return 0;
  const actionScore = actionTerms[candidate.actionType].some((term) => input.includes(term)) ? 2 : 0;
  return targetScore + actionScore;
}
