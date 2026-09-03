import { z } from "zod";
import type { StoryPackage } from "../../content/story-package.js";

const locationIdSchema = z.string().min(1);

export const branchStateSchema = z.object({
  playerLocationId: locationIdSchema,
  tangLocationId: locationIdSchema,
  jiangLocationId: locationIdSchema,
  tangStatus: z.enum(["missing", "located", "rescued"]),
  hasLockerToken: z.boolean(),
  evidenceStatus: z.enum(["unsecured", "secured"]),
  waterLevel: z.enum(["rising", "lowered"]),
  signalRoomStatus: z.enum(["locked", "opened"]),
  trainStatus: z.enum(["pending_release", "held", "departed"]),
});

export type BranchState = z.infer<typeof branchStateSchema>;

export const branchStatePatchSchema = z.object({
  playerLocationId: locationIdSchema.optional(),
  tangLocationId: locationIdSchema.optional(),
  jiangLocationId: locationIdSchema.optional(),
  tangStatus: z.enum(["missing", "located", "rescued"]).optional(),
  hasLockerToken: z.boolean().optional(),
  evidenceStatus: z.enum(["unsecured", "secured"]).optional(),
  waterLevel: z.enum(["rising", "lowered"]).optional(),
  signalRoomStatus: z.enum(["locked", "opened"]).optional(),
  trainStatus: z.enum(["pending_release", "held", "departed"]).optional(),
}).refine((patch) => Object.keys(patch).length > 0, "剧情方向必须声明至少一个状态变化");

export type BranchStatePatch = z.infer<typeof branchStatePatchSchema>;

export function applyBranchStatePatch(
  storyPackage: StoryPackage,
  current: BranchState,
  patch: BranchStatePatch,
  sourceNodeRef: string,
): BranchState {
  const next = branchStateSchema.parse({ ...current, ...patch });
  assertKnownLocations(storyPackage, next);
  assertMonotonicTransition(current, next);
  assertSceneLocation(storyPackage, next, sourceNodeRef);
  assertCharacterConsistency(next);
  return next;
}

function assertKnownLocations(storyPackage: StoryPackage, state: BranchState): void {
  const locationIds = new Set(storyPackage.locations.map((location) => location.id));
  for (const locationId of [state.playerLocationId, state.tangLocationId, state.jiangLocationId]) {
    if (!locationIds.has(locationId)) throw new Error(`共创状态引用了不存在的地点: ${locationId}`);
  }
}

function assertMonotonicTransition(current: BranchState, next: BranchState): void {
  assertRankDoesNotDecrease("唐栖状态", current.tangStatus, next.tangStatus, ["missing", "located", "rescued"]);
  assertRankDoesNotDecrease("证据状态", current.evidenceStatus, next.evidenceStatus, ["unsecured", "secured"]);
  assertRankDoesNotDecrease("水位处理状态", current.waterLevel, next.waterLevel, ["rising", "lowered"]);
  assertRankDoesNotDecrease("信号室状态", current.signalRoomStatus, next.signalRoomStatus, ["locked", "opened"]);
  assertRankDoesNotDecrease("列车状态", current.trainStatus, next.trainStatus, ["pending_release", "held", "departed"]);
  if (current.trainStatus === "held" && next.trainStatus === "departed") {
    throw new Error("已阻止放行的列车不能在共创状态中重新离站");
  }
}

function assertRankDoesNotDecrease(label: string, current: string, next: string, values: string[]): void {
  if (values.indexOf(next) < values.indexOf(current)) throw new Error(`${label}不能倒退: ${current} -> ${next}`);
}

function assertSceneLocation(storyPackage: StoryPackage, state: BranchState, sourceNodeRef: string): void {
  const sourceNode = storyPackage.story.nodes.find((node) => node.id === sourceNodeRef);
  if (!sourceNode) throw new Error(`共创状态引用的原著节点不存在: ${sourceNodeRef}`);
  const sceneLocation = sourceNode.contextRefs.find((reference) => storyPackage.locations.some((location) => location.id === reference));
  if (sceneLocation && state.playerLocationId !== sceneLocation) {
    throw new Error(`共创状态中的许川地点必须与场景一致: ${sceneLocation}`);
  }
}

function assertCharacterConsistency(state: BranchState): void {
  if (state.tangStatus === "rescued" && state.tangLocationId !== "location_waiting_hall") {
    throw new Error("唐栖获救后必须回到候车厅");
  }
  if (state.tangStatus !== "rescued" && state.tangLocationId !== "location_signal_tunnel") {
    throw new Error("唐栖未获救时必须仍在信号维修隧道区域");
  }
}
