import type { StoryPackage } from "../../content/story-package.js";
import type { BranchState } from "./branch-state.js";
import type { BranchNode } from "./branch-node.js";

type BranchDirection = BranchNode["nextDirections"][number];

export type ResolvedRejoin = {
  id: string;
  targetBeatId: string;
};

export function resolveRejoin(
  storyPackage: StoryPackage,
  parentSourceNodeId: string,
  parentOpenThreads: string[],
  targetNodeId: string,
  resolvedState: BranchState,
  direction: BranchDirection,
): ResolvedRejoin | undefined {
  if (!direction.rejoinTargetId) return undefined;

  const target = storyPackage.story.rejoinTargets.find((candidate) => candidate.id === direction.rejoinTargetId);
  if (!target) throw new Error(`方向引用的汇合目标不存在: ${direction.rejoinTargetId}`);
  if (target.fromNodeId !== parentSourceNodeId) {
    throw new Error(`汇合目标的来源场景不匹配: ${target.fromNodeId}`);
  }
  if (!target.requiredOpenThreads.every((thread) => parentOpenThreads.includes(thread))) {
    throw new Error(`汇合目标缺少必要未解线索: ${target.id}`);
  }

  const targetBeat = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === target.targetBeatId);
  if (!targetBeat) throw new Error(`汇合目标锚点不存在: ${target.targetBeatId}`);
  if (targetBeat.nodeId !== targetNodeId) {
    throw new Error(`汇合目标场景与受控路线不一致: ${target.id}`);
  }
  if (!sameBranchState(targetBeat.branchState, resolvedState)) {
    throw new Error(`汇合目标状态不兼容: ${target.id}`);
  }

  return { id: target.id, targetBeatId: target.targetBeatId };
}

function sameBranchState(left: BranchState, right: BranchState): boolean {
  return Object.keys(left).every((key) => left[key as keyof BranchState] === right[key as keyof BranchState]);
}
