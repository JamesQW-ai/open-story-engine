import type { GameState, StoryPackage } from "../../content/story-package.js";
import { matchesStateCondition } from "../state/state-conditions.js";

export type NarrativeContinuity = {
  beatId: string;
  summary: string;
  openThreads: string[];
  currentNodeId: string;
  currentLocationId: string;
};

export type NarrativeSelection = {
  beat: StoryPackage["story"]["narrativeGraph"]["beats"][number];
  transition?: StoryPackage["story"]["narrativeGraph"]["edges"][number];
  continuity: NarrativeContinuity;
  changedBeat: boolean;
};

export function selectNarrativeProgression(
  storyPackage: StoryPackage,
  state: GameState,
  previousContinuity: NarrativeContinuity | undefined,
  endingId: string | undefined,
): NarrativeSelection {
  const graph = storyPackage.story.narrativeGraph;
  const beatsById = new Map(graph.beats.map((beat) => [beat.id, beat]));
  const startBeat = beatsById.get(graph.startBeatId);
  if (!startBeat) throw new Error(`叙事图起始锚点不存在: ${graph.startBeatId}`);

  const currentBeat = beatsById.get(previousContinuity?.beatId ?? graph.startBeatId) ?? startBeat;
  const endingBeatId = endingId ? graph.endingBeatIds[endingId] : undefined;
  const endingBeat = endingBeatId ? beatsById.get(endingBeatId) : undefined;
  const transition = endingBeat
    ? undefined
    : graph.edges.find(
      (edge) => edge.fromBeatId === currentBeat.id && edge.when.every((condition) => matchesStateCondition(state, condition)),
    );
  const beat = endingBeat ?? (transition ? beatsById.get(transition.toBeatId) : currentBeat);
  if (!beat) throw new Error("叙事图边指向了不存在的锚点");

  return {
    beat,
    transition,
    changedBeat: beat.id !== currentBeat.id,
    continuity: {
      beatId: beat.id,
      summary: beat.summary,
      openThreads: [...beat.openThreads],
      currentNodeId: state.currentNodeId,
      currentLocationId: state.currentLocationId,
    },
  };
}
