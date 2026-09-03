import type { GameState, StoryPackage } from "../../content/story-package.js";
import { matchesStateCondition } from "../state/state-conditions.js";
import type { NarrativeContinuity } from "./narrative-progression.js";

export type NarrativeDirection = {
  id: string;
  title: string;
  summary: string;
  suggestedInput: string;
};

export function listNarrativeDirections(
  storyPackage: StoryPackage,
  state: GameState,
  continuity?: NarrativeContinuity,
): NarrativeDirection[] {
  const graph = storyPackage.story.narrativeGraph;
  const beatId = continuity?.beatId ?? graph.startBeatId;
  const beat = graph.beats.find((candidate) => candidate.id === beatId);
  if (!beat) throw new Error(`叙事方向对应的锚点不存在: ${beatId}`);

  return beat.nextDirections
    .filter((direction) => direction.when.every((condition) => matchesStateCondition(state, condition)))
    .map(({ id, title, summary, suggestedInput }) => ({ id, title, summary, suggestedInput }));
}
