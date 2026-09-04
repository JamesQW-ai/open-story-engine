import { randomUUID } from "node:crypto";
import { z } from "zod";
import type { StoryPackage } from "../../content/story-package.js";
import { branchStatePatchSchema, branchStateSchema } from "./branch-state.js";
import { narrativePlanSchema } from "./narrative-plan.js";
import type { SessionStoryContract } from "./session-story-contract.js";

const idSchema = z.string().min(1).regex(/^[A-Za-z0-9_-]+$/);

export const branchDirectionSchema = z.object({
  id: idSchema,
  title: z.string().min(1),
  summary: z.string().min(1),
  canonicalBeatId: idSchema.optional(),
  rejoinTargetId: idSchema.optional(),
  statePatch: branchStatePatchSchema,
});

export const branchFactDeltaSchema = z.object({
  id: idSchema,
  source: z.enum(["source", "user", "derived"]),
  summary: z.string().min(1),
});

const stateChangeProposalSchema = z.object({
  summary: z.string().min(1).optional(),
  proposal: z.string().min(1).optional(),
  rationale: z.string().min(1),
}).transform((value) => ({
  // 状态建议只用于审计；缺省摘要时保留模型已经给出的理由，不能影响分支状态校验。
  summary: value.summary ?? value.proposal ?? value.rationale,
  rationale: value.rationale,
}));

export const branchPlanningSchema = z.object({
  citations: z.array(z.object({
    kind: z.enum(["immutable_fact", "canonical_node", "branch_node"]),
    ref: idSchema,
    rationale: z.string().min(1),
  })).min(1),
  confidence: z.enum(["high", "medium", "low"]),
  stateChangeProposals: z.array(stateChangeProposalSchema),
});

export const storyArcSchema = z.object({
  activeGoal: z.string().min(1).max(500),
  currentPhase: z.string().min(1).max(300),
  goalDisposition: z.enum(["started", "continued", "replaced", "completed"]),
  chapter: z.object({
    title: z.string().min(1).max(120),
    status: z.enum(["continuing", "complete"]),
  }),
});

export type StoryArc = z.infer<typeof storyArcSchema>;

export const branchNodeSchema = z.object({
  id: idSchema,
  kind: z.enum(["source_entry", "generated"]),
  parentId: idSchema.optional(),
  sourceNodeRef: idSchema.optional(),
  branchState: branchStateSchema,
  requestId: idSchema.optional(),
  selectedDirectionId: idSchema.optional(),
  playerDirection: z.string().min(1).optional(),
  narrativeText: z.string().min(1),
  summary: z.string().min(1),
  factDeltas: z.array(branchFactDeltaSchema),
  openThreads: z.array(z.string().min(1)),
  nextDirections: z.array(branchDirectionSchema),
  canonicalRelation: z.enum(["on_line", "diverged", "rejoined"]),
  narrativePlan: narrativePlanSchema.optional(),
  storyArc: storyArcSchema.optional(),
  planning: branchPlanningSchema,
  createdAt: z.string().datetime(),
});

export type BranchNode = z.infer<typeof branchNodeSchema>;

export function createEntryBranchNode(storyPackage: StoryPackage, contract: SessionStoryContract): BranchNode {
  const beat = storyPackage.story.narrativeGraph.beats.find(
    (candidate) => candidate.nodeId === contract.entryNodeId && candidate.id === storyPackage.story.narrativeGraph.startBeatId,
  ) ?? storyPackage.story.narrativeGraph.beats.find((candidate) => candidate.nodeId === contract.entryNodeId);
  if (!beat) throw new Error(`共创起始节点没有叙事锚点: ${contract.entryNodeId}`);

  return branchNodeSchema.parse({
    id: `branch_${randomUUID()}`,
    kind: "source_entry",
    sourceNodeRef: contract.entryNodeId,
    branchState: beat.branchState,
    narrativeText: beat.sourceExcerpt?.text ?? beat.narrativeAnchor,
    summary: beat.summary,
    factDeltas: [{ id: "fact_source_entry", source: "source", summary: `原著前史已继承至 ${contract.entryNodeId}` }],
    openThreads: beat.openThreads,
    nextDirections: beat.nextDirections.map(({ id, title, summary, canonicalBeatId, rejoinTargetId, statePatch }) => ({ id, title, summary, canonicalBeatId, rejoinTargetId, statePatch })),
    canonicalRelation: "on_line",
    planning: {
      citations: [{ kind: "canonical_node", ref: contract.entryNodeId, rationale: "共创从此原著节点进入。" }],
      confidence: "high",
      stateChangeProposals: [],
    },
    createdAt: new Date().toISOString(),
  });
}

export function parseBranchNode(input: unknown): BranchNode {
  return branchNodeSchema.parse(input);
}
