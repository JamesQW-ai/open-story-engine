import { z } from "zod";

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

export function parseDirectionEvaluation(input: unknown): DirectionEvaluation {
  return directionEvaluationSchema.parse(input);
}
