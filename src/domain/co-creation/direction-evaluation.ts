import { z } from "zod";
import type { CoCreationContext } from "./context-builder.js";
import type { LlmCallObservation } from "../llm/llm-gateway.js";

export const directionEvaluationSchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("accepted"),
    directionId: z.string().min(1),
    rationale: z.string().min(1),
  }),
  z.object({
    kind: z.literal("clarification_needed"),
    message: z.string().min(1),
  }),
  z.object({
    kind: z.literal("rejected"),
    message: z.string().min(1),
    citations: z.array(z.object({ kind: z.literal("immutable_fact"), ref: z.string().min(1) })).min(1),
  }),
]);

export type DirectionEvaluation = z.infer<typeof directionEvaluationSchema>;

export type DirectionEvaluationAudit = {
  operation: "direction_evaluator";
  model: string;
  promptVersion: string;
  requestSummary: string;
  rawResponse?: string;
  error?: string;
  callObservations?: LlmCallObservation[];
};

export type DirectionEvaluationExecution = {
  evaluation: DirectionEvaluation;
  audit?: DirectionEvaluationAudit;
};

export interface DirectionEvaluator {
  evaluate(context: CoCreationContext, playerDirection: string): Promise<DirectionEvaluationExecution>;
}

export function parseDirectionEvaluation(input: unknown): DirectionEvaluation {
  return directionEvaluationSchema.parse(input);
}

export function assertDirectionEvaluationFitsContext(
  evaluation: DirectionEvaluation,
  context: CoCreationContext,
): DirectionEvaluation {
  if (evaluation.kind === "accepted" && !context.parent.nextDirections.some((direction) => direction.id === evaluation.directionId)) {
    throw new Error(`自由文本判定引用了当前未公布的剧情方向: ${evaluation.directionId}`);
  }

  if (evaluation.kind === "rejected") {
    const immutableReferences = new Set(
      context.availableReferences
        .filter((reference) => reference.kind === "immutable_fact")
        .map((reference) => reference.ref),
    );
    for (const citation of evaluation.citations) {
      if (!immutableReferences.has(citation.ref)) {
        throw new Error(`自由文本判定引用了上下文外的不可变事实: ${citation.ref}`);
      }
    }
  }

  return evaluation;
}
