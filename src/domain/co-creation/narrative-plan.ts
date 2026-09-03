import { z } from "zod";
import type { StoryPackage } from "../../content/story-package.js";
import { branchStatePatchSchema, type BranchState, type BranchStatePatch } from "./branch-state.js";

const idSchema = z.string().min(1).regex(/^[A-Za-z0-9_-]+$/);

const narrativePlanBeatSchema = z.object({
  kind: z.enum(["establish_constraint", "transition_scene", "advance_direction"]),
  summary: z.string().min(1),
  sourceNodeRef: idSchema,
});

export const narrativePlanSchema = z.object({
  directionId: idSchema,
  goal: z.string().min(1),
  sourceNodeRef: idSchema,
  targetNodeRef: idSchema,
  selectedStatePatch: branchStatePatchSchema,
  beats: z.array(narrativePlanBeatSchema).min(2).max(3),
}).superRefine((plan, context) => {
  if (plan.beats[0]?.kind !== "establish_constraint") {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["beats", 0], message: "叙事计划必须先承接当前约束" });
  }
  if (plan.beats.at(-1)?.kind !== "advance_direction") {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["beats"], message: "叙事计划必须以落实已选方向结束" });
  }
  const includesTransition = plan.beats.some((beat) => beat.kind === "transition_scene");
  if ((plan.sourceNodeRef !== plan.targetNodeRef) !== includesTransition) {
    context.addIssue({ code: z.ZodIssueCode.custom, path: ["beats"], message: "跨场景计划必须且只能包含一个场景转移节拍" });
  }
});

export type NarrativePlan = z.infer<typeof narrativePlanSchema>;

type NarrativePlanDirection = {
  id: string;
  title: string;
  summary: string;
  statePatch: BranchStatePatch;
};

export function buildNarrativePlan(
  storyPackage: StoryPackage,
  input: {
    direction: NarrativePlanDirection;
    currentState: BranchState;
    sourceNodeRef: string;
    targetNodeRef: string;
    openThreads: string[];
  },
): NarrativePlan {
  const currentLocation = findLocation(storyPackage, input.currentState.playerLocationId);
  const targetLocationId = input.direction.statePatch.playerLocationId ?? input.currentState.playerLocationId;
  const targetLocation = findLocation(storyPackage, targetLocationId);
  const targetNode = storyPackage.story.nodes.find((node) => node.id === input.targetNodeRef);
  if (!targetNode) throw new Error(`叙事计划目标场景不存在: ${input.targetNodeRef}`);

  const openThreads = input.openThreads.slice(0, 2);
  const beats: NarrativePlan["beats"] = [{
    kind: "establish_constraint",
    sourceNodeRef: input.sourceNodeRef,
    summary: openThreads.length > 0
      ? `承接${currentLocation.name}的既有局面，并围绕未解线索推进：${openThreads.join("、")}。`
      : `承接${currentLocation.name}的既有局面，不新增未确认事实。`,
  }];

  if (input.sourceNodeRef !== input.targetNodeRef) {
    beats.push({
      kind: "transition_scene",
      sourceNodeRef: input.targetNodeRef,
      summary: `让许川从${currentLocation.name}转至${targetLocation.name}，场景范围切换为“${targetNode.objective}”。`,
    });
  }

  beats.push({
    kind: "advance_direction",
    sourceNodeRef: input.targetNodeRef,
    summary: `落实方向“${input.direction.title}”：${input.direction.summary}`,
  });

  return narrativePlanSchema.parse({
    directionId: input.direction.id,
    goal: input.direction.summary,
    sourceNodeRef: input.sourceNodeRef,
    targetNodeRef: input.targetNodeRef,
    selectedStatePatch: input.direction.statePatch,
    beats,
  });
}

function findLocation(storyPackage: StoryPackage, locationId: string) {
  const location = storyPackage.locations.find((entry) => entry.id === locationId);
  if (!location) throw new Error(`叙事计划地点不存在: ${locationId}`);
  return location;
}
