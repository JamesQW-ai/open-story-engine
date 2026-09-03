import type { StoryPackage } from "../../content/story-package.js";
import type { BranchStatePatch } from "./branch-state.js";

export function resolveSceneNodeId(
  storyPackage: StoryPackage,
  currentNodeId: string,
  currentLocationId: string,
  patch: BranchStatePatch,
): string {
  const targetLocationId = patch.playerLocationId ?? currentLocationId;
  if (targetLocationId === currentLocationId) return currentNodeId;

  const route = storyPackage.story.sceneRoutes.find((candidate) => (
    candidate.fromNodeId === currentNodeId
    && candidate.fromLocationId === currentLocationId
    && candidate.toLocationId === targetLocationId
  ));
  if (!route) {
    throw new Error(`当前场景没有到达目标地点的受控路线: ${currentNodeId} ${currentLocationId} -> ${targetLocationId}`);
  }
  return route.toNodeId;
}
