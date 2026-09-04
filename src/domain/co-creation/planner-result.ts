import { z } from "zod";
import { branchDirectionSchema, branchFactDeltaSchema, branchPlanningSchema, storyArcSchema } from "./branch-node.js";
import type { BranchState } from "./branch-state.js";
import type { CoCreationContext } from "./context-builder.js";

export const plannerResultSchema = z.object({
  // This is a technical guardrail, not a literary pacing requirement.
  narrativeText: z.string().min(1).max(12_000),
  summary: z.string().min(1),
  factDeltas: z.array(branchFactDeltaSchema),
  openThreads: z.array(z.string().min(1)),
  nextDirections: z.array(branchDirectionSchema),
  canonicalRelation: z.enum(["on_line", "diverged", "rejoined"]),
  storyArc: storyArcSchema,
  planning: branchPlanningSchema,
});

export type PlannerResult = z.infer<typeof plannerResultSchema>;

export function assertPlannerResultFitsContext(result: PlannerResult, context: CoCreationContext, resolvedState: BranchState): PlannerResult {
  const availableReferences = new Set(context.availableReferences.map((reference) => `${reference.kind}:${reference.ref}`));
  for (const citation of result.planning.citations) {
    if (!availableReferences.has(`${citation.kind}:${citation.ref}`)) {
      throw new Error(`规划结果引用了当前上下文之外的内容: ${citation.kind}:${citation.ref}`);
    }
  }
  for (const direction of result.nextDirections) {
    if (direction.rejoinTargetId && !context.rejoinTargets.some((target) => target.id === direction.rejoinTargetId)) {
      throw new Error(`规划结果引用了当前场景之外的汇合目标: ${direction.rejoinTargetId}`);
    }
    if (!Object.entries(direction.statePatch).some(([field, value]) => resolvedState[field as keyof BranchState] !== value)) {
      throw new Error(`剧情方向没有推进任何受控状态: ${direction.id}`);
    }
  }
  return result;
}
