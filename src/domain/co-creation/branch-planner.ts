import type { CoCreationContext } from "./context-builder.js";
import type { BranchState } from "./branch-state.js";
import type { NarrativePlan } from "./narrative-plan.js";
import type { PlannerResult } from "./planner-result.js";
import type { LlmCallObservation } from "../llm/llm-gateway.js";

export type BranchPlanRequest = {
  context: CoCreationContext;
  selectedDirectionId: string;
  sourceNodeRef: string;
  resolvedState: BranchState;
  narrativePlan: NarrativePlan;
  playerDirection?: string;
};

export type PlannerAudit = {
  operation: "branch_planner";
  model: string;
  promptVersion: string;
  requestSummary: string;
  rawResponse?: string;
  error?: string;
  callObservations?: LlmCallObservation[];
};

export type PlannerExecution =
  | { kind: "completed"; result: PlannerResult; audit?: PlannerAudit }
  | { kind: "failed"; message: string; audit: PlannerAudit };

export interface BranchPlanner {
  plan(request: BranchPlanRequest): Promise<PlannerExecution>;
}
