import type { GameState, StoryPackage } from "../../content/story-package.js";
import type { ActionIntent, TurnResolution } from "../rules/rule-engine.js";
import type { NarrativeSelection } from "./narrative-progression.js";

export type NarrationInput = {
  storyPackage: StoryPackage;
  playerInput: string;
  intent: ActionIntent;
  previousState: Readonly<GameState>;
  resolution: Readonly<TurnResolution>;
  progression: NarrativeSelection;
};

export interface Narrator {
  narrate(input: NarrationInput): string;
}

export class MockNarrator implements Narrator {
  narrate(input: NarrationInput): string {
    const { storyPackage, intent, resolution } = input;
    const targetName = findEntityName(storyPackage, intent.targetId) ?? intent.targetId;
    const { progression } = input;

    if (resolution.outcome === "blocked" || resolution.outcome === "terminal") {
      return `许川的注意力落在${targetName}上，但${resolution.blockReason ?? "眼前的处境还不允许他这样做"}。${progression.continuity.summary}`;
    }

    const outcome = resolution.outcome === "partial_success"
        ? "事情勉强有了进展，代价也随着雨水一起逼近。"
        : resolution.outcome === "failure"
          ? "这一步没能打开局面。"
          : "";
    // A changed beat already encodes this turn's story consequence. Repeating
    // low-level effects here makes one newly generated paragraph read like a log.
    const confirmedChanges = progression.changedBeat ? "" : describeConfirmedChanges(storyPackage, resolution);
    const bridge = progression.transition?.transitionText ?? `许川朝${targetName}迈出一步。`;
    const anchor = progression.changedBeat ? progression.beat.narrativeAnchor : "";
    const ending = resolution.endingId && !progression.changedBeat
      ? ` ${storyPackage.story.endings.find((candidate) => candidate.id === resolution.endingId)?.narrativeAnchor ?? "这一夜的结果已经无法再被改写。"}`
      : "";
    return `${bridge}${outcome}${confirmedChanges}${anchor}${ending}`;
  }
}

function findEntityName(storyPackage: StoryPackage, targetId: string): string | undefined {
  return [
    ...storyPackage.items,
    ...storyPackage.locations,
    ...storyPackage.characters,
  ].find((entity) => entity.id === targetId)?.name;
}

function describeConfirmedChanges(storyPackage: StoryPackage, resolution: Readonly<TurnResolution>): string {
  const additions = resolution.appliedEffects
    .filter((effect) => effect.startsWith("add:"))
    .map((effect) => findEntityName(storyPackage, effect.slice("add:".length)))
    .filter((name): name is string => Boolean(name));
  const movement = resolution.appliedEffects.find((effect) => effect.startsWith("move:"));
  const movedTo = movement ? findEntityName(storyPackage, movement.slice("move:".length)) : undefined;
  const parts: string[] = [];
  if (additions.length > 0) parts.push(`他确认拿到了${additions.join("、")}。`);
  if (movedTo) parts.push(`随后，他抵达了${movedTo}。`);
  if (resolution.appliedEffects.some((effect) => effect === "increment:pressure_level")) parts.push("列车放行的压力因此更近了一层。");
  if (resolution.appliedEffects.some((effect) => effect === "increment:flood_level")) parts.push("隧道里的水位又向上逼近了一点。");
  const flagSentences: Record<string, string> = {
    "set:flag_office_access": "通往站务室的封锁已经被跨过。",
    "set:flag_evidence_secured": "唐栖留下的证据已经被许川保住。",
    "set:flag_log_tampered": "被篡改的记录终于露出了痕迹。",
    "set:flag_tang_located": "唐栖被困在信号室的事实已经得到确认。",
    "set:flag_tunnel_access": "通往隧道的路不再只是猜测。",
    "set:flag_relief_valve_opened": "排水通路被重新打开，积水暂时没有再逼近。",
    "set:flag_signal_door_opened": "卡住的滑栓终于松开。",
    "set:flag_tang_rescued": "唐栖终于离开了信号室。",
    "set:flag_train_departure_held": "司机没有接受放行指令，末班列车仍停在雨里。",
    "set:flag_chen_yan_confronted": "陈砚再也无法把这份证词当作不存在。",
  };
  resolution.appliedEffects.forEach((effect) => {
    const sentence = flagSentences[effect];
    if (sentence) parts.push(sentence);
  });
  return parts.length > 0 ? parts.join("") : "局面没有产生规则外的变化，许川仍需要寻找下一处突破口。";
}
