import type { GameState, StoryPackage } from "../../content/story-package.js";
import { matchesStateCondition } from "../state/state-conditions.js";

export type ActionIntent = {
  actionType: StoryPackage["rules"]["actionTypes"][number];
  targetId: string;
  approach: string;
};

export type RandomSource = { nextInt(min: number, max: number): number };
export type TurnOutcome = "success" | "partial_success" | "failure" | "blocked" | "terminal";

export type TurnResolution = {
  outcome: TurnOutcome;
  action: ActionIntent;
  state: GameState;
  resolutionId?: string;
  roll?: number;
  score?: number;
  appliedEffects: string[];
  endingId?: string;
  blockReason?: string;
};

export class RuleEngine {
  constructor(private readonly storyPackage: StoryPackage) {}

  resolve(state: GameState, action: ActionIntent, random: RandomSource): TurnResolution {
    const nextState = structuredClone(state);
    const existingEnding = this.findEnding(nextState);
    if (existingEnding) return this.terminalResolution(action, nextState, existingEnding.id);

    const candidates = this.storyPackage.rules.resolutions.filter(
      (candidate) => candidate.nodeId === nextState.currentNodeId && candidate.actionType === action.actionType && candidate.targetId === action.targetId,
    );
    if (candidates.length === 0) {
      return { outcome: "blocked", action, state: nextState, appliedEffects: [], blockReason: "当前节点没有可匹配的规则行动" };
    }
    const resolution = candidates.find((candidate) => candidate.guards.every((guard) => matchesStateCondition(nextState, guard)));
    if (!resolution) {
      return { outcome: "blocked", action, state: nextState, appliedEffects: [], blockReason: "行动前置条件未满足" };
    }

    const { min, max } = this.storyPackage.rules.check.randomRange;
    const roll = random.nextInt(min, max);
    if (!Number.isInteger(roll) || roll < min || roll > max) throw new Error(`随机源返回了范围外的值: ${roll}`);

    const score = nextState.player.attributes[resolution.attribute] + roll;
    const successThreshold = this.storyPackage.rules.check.successAt + resolution.difficulty - 1;
    const partialThreshold = this.storyPackage.rules.check.partialSuccessAt + resolution.difficulty - 1;
    const outcome: Extract<TurnOutcome, "success" | "partial_success" | "failure"> = score >= successThreshold ? "success" : score >= partialThreshold ? "partial_success" : "failure";
    const effects = outcome === "success" ? resolution.onSuccess : outcome === "partial_success" ? resolution.onPartial : resolution.onFailure;

    this.applyEffects(nextState, effects);
    this.applyTerminalRules(nextState);
    this.advanceNode(nextState);
    const ending = this.findEnding(nextState);

    return { outcome, action, state: nextState, resolutionId: resolution.id, roll, score, appliedEffects: [...effects], endingId: ending?.id };
  }

  private terminalResolution(action: ActionIntent, state: GameState, endingId: string): TurnResolution {
    return { outcome: "terminal", action, state, appliedEffects: [], endingId, blockReason: "当前会话已达到结局，不能继续执行普通行动" };
  }

  private advanceNode(state: GameState): void {
    const node = this.storyPackage.story.nodes.find((candidate) => candidate.id === state.currentNodeId);
    const transition = node?.transitions.find((candidate) => candidate.when.every((condition) => matchesStateCondition(state, condition)));
    if (transition) state.currentNodeId = transition.toNodeId;
  }

  private applyTerminalRules(state: GameState): void {
    for (const terminalRule of this.storyPackage.rules.terminalRules) {
      if (terminalRule.when.every((condition) => matchesStateCondition(state, condition))) this.applyEffects(state, terminalRule.set);
    }
  }

  private findEnding(state: GameState): StoryPackage["story"]["endings"][number] | undefined {
    return this.storyPackage.story.endings.find((ending) => ending.when.every((condition) => matchesStateCondition(state, condition)));
  }

  private applyEffects(state: GameState, effects: string[]): void {
    for (const effect of effects) {
      if (effect.startsWith("add:")) {
        const itemId = effect.slice("add:".length);
        if (!state.inventory.includes(itemId)) state.inventory.push(itemId);
        continue;
      }
      if (effect.startsWith("move:")) {
        state.currentLocationId = effect.slice("move:".length);
        continue;
      }
      if (effect.startsWith("increment:")) {
        const counter = effect.slice("increment:".length);
        const value = state.counters[counter];
        if (value === undefined) throw new Error(`未知计数器: ${counter}`);
        state.counters[counter] = value + 1;
        continue;
      }
      if (effect.startsWith("set:relationship.")) {
        const [characterId, rawValue] = effect.slice("set:relationship.".length).split("=");
        if (!characterId || rawValue === undefined || !Number.isInteger(Number(rawValue))) throw new Error(`无效关系效果: ${effect}`);
        state.relationships[characterId] = Number(rawValue);
        continue;
      }
      if (effect.startsWith("set:")) {
        const [field, rawValue] = effect.slice("set:".length).split("=");
        if (!field) throw new Error(`无效设置效果: ${effect}`);
        if (rawValue === undefined) {
          if (!(field in state.flags)) throw new Error(`未知旗标: ${field}`);
          state.flags[field] = true;
          continue;
        }
        if (field in state.counters && Number.isInteger(Number(rawValue))) {
          state.counters[field] = Number(rawValue);
          continue;
        }
        if (field in state.flags && (rawValue === "true" || rawValue === "false")) {
          state.flags[field] = rawValue === "true";
          continue;
        }
      }
      throw new Error(`不支持的状态效果: ${effect}`);
    }
  }
}
