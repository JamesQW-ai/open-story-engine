import type { StoryPackage } from "../../content/story-package.js";
import type { BranchNode } from "./branch-node.js";

export type ResolvedCanonicalBranchNode = Omit<BranchNode, "id" | "kind" | "parentId" | "createdAt">;

export function createCanonicalBranch(
  storyPackage: StoryPackage,
  direction: BranchNode["nextDirections"][number],
): ResolvedCanonicalBranchNode | undefined {
  if (!direction.canonicalBeatId) return undefined;
  const beat = storyPackage.story.narrativeGraph.beats.find((candidate) => candidate.id === direction.canonicalBeatId);
  if (!beat?.sourceExcerpt) throw new Error(`规范方向缺少可复用原文: ${direction.id}`);

  return {
    sourceNodeRef: beat.nodeId,
    branchState: beat.branchState,
    narrativeText: beat.sourceExcerpt.text,
    summary: beat.summary,
    factDeltas: [{ id: `fact_source_${beat.id}`, source: "source", summary: `沿原著推进至 ${beat.id}。` }],
    openThreads: beat.openThreads,
    nextDirections: beat.nextDirections.map(({ id, title, summary, canonicalBeatId, rejoinTargetId, statePatch }) => ({ id, title, summary, canonicalBeatId, rejoinTargetId, statePatch })),
    canonicalRelation: "on_line",
    planning: {
      citations: [{ kind: "canonical_node", ref: beat.nodeId, rationale: "当前分支未偏离原著，所选方向可直接复用该原文节点。" }],
      confidence: "high",
      stateChangeProposals: [],
    },
  };
}
