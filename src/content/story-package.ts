import { readFile } from "node:fs/promises";
import { z } from "zod";
import { branchStatePatchSchema, branchStateSchema } from "../domain/co-creation/branch-state.js";

const idSchema = z.string().min(1);
const conditionSchema = z.string().min(1);
const effectSchema = z.string().min(1);
const actionTypeSchema = z.enum(["investigate", "negotiate", "risk"]);
const attributeSchema = z.enum(["insight", "empathy", "nerve"]);

const characterSchema = z.object({
  id: idSchema,
  name: z.string().min(1),
  role: z.string().min(1),
  description: z.string().min(1),
  initialLocationId: idSchema,
  hiddenInfo: z.object({ unlockWhen: z.array(conditionSchema).min(1) }).optional(),
  tags: z.array(z.string().min(1)),
});

const locationSchema = z.object({
  id: idSchema,
  name: z.string().min(1),
  description: z.string().min(1),
  exits: z.array(idSchema),
  discoverableItemIds: z.array(idSchema),
  tags: z.array(z.string().min(1)),
});

const itemSchema = z.object({
  id: idSchema,
  name: z.string().min(1),
  portable: z.boolean(),
  initialHolderId: idSchema.optional(),
  initialLocationId: idSchema.optional(),
  effects: z.array(z.string().min(1)),
}).refine(
  (item) => Boolean(item.initialHolderId) !== Boolean(item.initialLocationId),
  "物品必须且只能指定一个初始持有者或初始地点",
);

const storyNodeSchema = z.object({
  id: idSchema,
  objective: z.string().min(1),
  sceneSetup: z.array(z.string().min(1)).min(1),
  requiredProgress: z.array(conditionSchema),
  pressure: z.object({ field: idSchema, terminalAt: z.number().int().positive(), effect: effectSchema }),
  transitions: z.array(z.object({ toNodeId: idSchema, when: z.array(conditionSchema).min(1), canonical: z.boolean() })),
  contextRefs: z.array(idSchema),
});

const endingSchema = z.object({
  id: idSchema,
  title: z.string().min(1),
  when: z.array(conditionSchema).min(1),
  canonical: z.boolean(),
  narrativeAnchor: z.string().min(1),
});

const narrativeBeatSchema = z.object({
  id: idSchema,
  nodeId: idSchema,
  summary: z.string().min(1),
  sourceExcerpt: z.object({
    text: z.string().min(1),
    lineRange: z.tuple([z.number().int().positive(), z.number().int().positive()]),
  }).optional(),
  branchState: branchStateSchema,
  narrativeAnchor: z.string().min(1),
  openThreads: z.array(z.string().min(1)),
  nextDirections: z.array(z.object({
    id: idSchema,
    title: z.string().min(1),
    summary: z.string().min(1),
    canonicalBeatId: idSchema.optional(),
    rejoinTargetId: idSchema.optional(),
    statePatch: branchStatePatchSchema,
    when: z.array(conditionSchema).default([]),
    suggestedInput: z.string().min(1),
  })),
});

const narrativeEdgeSchema = z.object({
  id: idSchema,
  fromBeatId: idSchema,
  toBeatId: idSchema,
  when: z.array(conditionSchema).min(1),
  canonical: z.boolean(),
  transitionText: z.string().min(1),
});

const sceneRouteSchema = z.object({
  fromNodeId: idSchema,
  fromLocationId: idSchema,
  toLocationId: idSchema,
  toNodeId: idSchema,
});

const rejoinTargetSchema = z.object({
  id: idSchema,
  fromNodeId: idSchema,
  targetBeatId: idSchema,
  requiredOpenThreads: z.array(z.string().min(1)).min(1),
});

const resolutionSchema = z.object({
  id: idSchema,
  nodeId: idSchema,
  actionType: actionTypeSchema,
  targetId: idSchema,
  attribute: attributeSchema,
  difficulty: z.number().int().min(1),
  guards: z.array(conditionSchema).optional().default([]),
  onSuccess: z.array(effectSchema),
  onPartial: z.array(effectSchema),
  onFailure: z.array(effectSchema),
});

export const gameStateSchema = z.object({
  player: z.object({
    characterId: idSchema,
    attributes: z.object({ insight: z.number().int(), empathy: z.number().int(), nerve: z.number().int() }),
  }),
  inventory: z.array(idSchema),
  relationships: z.record(z.number().int()),
  flags: z.record(z.boolean()),
  counters: z.record(z.number().int()),
  knownFacts: z.array(idSchema),
  currentNodeId: idSchema,
  currentLocationId: idSchema,
  directionId: idSchema,
});

export const storyPackageSchema = z.object({
  schemaVersion: z.literal("1.0"),
  id: idSchema,
  version: z.string().min(1),
  metadata: z.object({
    title: z.string().min(1), summary: z.string().min(1), authoringSource: z.literal("original"), contentRating: z.string().min(1), language: z.string().min(1),
  }),
  world: z.object({
    premise: z.string().min(1),
    immutableFacts: z.array(z.object({ id: idSchema, text: z.string().min(1) })),
    narrativeGuidelines: z.object({
      perspective: z.literal("third_person_limited"), focalCharacterId: idSchema, tense: z.string().min(1), language: z.string().min(1),
      turnLengthCharacters: z.object({ min: z.number().int().positive(), max: z.number().int().positive() }),
      prohibitions: z.array(z.string().min(1)).min(1),
    }),
    globalConstraints: z.array(z.string().min(1)).min(1),
  }),
  characters: z.array(characterSchema).min(1),
  locations: z.array(locationSchema).min(1),
  items: z.array(itemSchema).min(1),
  timeline: z.array(z.object({ id: idSchema, order: z.number().int().positive(), knownAtStart: z.boolean(), description: z.string().min(1) })),
  story: z.object({
    mode: z.literal("mainline"), premise: z.string().min(1), longTermGoal: z.string().min(1), startNodeId: idSchema,
    nodes: z.array(storyNodeSchema).min(1),
    endings: z.array(endingSchema).min(1),
    narrativeGraph: z.object({
      startBeatId: idSchema,
      beats: z.array(narrativeBeatSchema).min(1),
      edges: z.array(narrativeEdgeSchema).min(1),
      endingBeatIds: z.record(idSchema),
    }),
    sceneRoutes: z.array(sceneRouteSchema).min(1),
    rejoinTargets: z.array(rejoinTargetSchema).default([]),
  }),
  directions: z.array(z.object({
    id: idSchema, title: z.string().min(1), summary: z.string().min(1), primaryGoal: z.string().min(1),
    preferredNodeIds: z.array(idSchema).min(1), reachableEndingIds: z.array(idSchema).min(1),
  })).min(1),
  defaultDirectionId: idSchema,
  rules: z.object({
    actionTypes: z.array(actionTypeSchema).min(1),
    check: z.object({
      randomRange: z.object({ min: z.number().int(), max: z.number().int() }),
      successAt: z.number().int().positive(), partialSuccessAt: z.number().int().positive(),
    }).superRefine((check, context) => {
      if (check.randomRange.min > check.randomRange.max) context.addIssue({ code: z.ZodIssueCode.custom, message: "随机范围最小值不能大于最大值" });
      if (check.partialSuccessAt > check.successAt) context.addIssue({ code: z.ZodIssueCode.custom, message: "部分成功阈值不能高于成功阈值" });
    }),
    attributes: z.array(attributeSchema).min(1),
    resolutions: z.array(resolutionSchema).min(1),
    guards: z.array(z.string().min(1)),
    terminalRules: z.array(z.object({ when: z.array(conditionSchema).min(1), set: z.array(effectSchema).min(1) })),
  }),
  initialState: gameStateSchema,
}).superRefine((storyPackage, context) => {
  const ids = {
    character: new Set(storyPackage.characters.map((entry) => entry.id)),
    location: new Set(storyPackage.locations.map((entry) => entry.id)),
    item: new Set(storyPackage.items.map((entry) => entry.id)),
    timeline: new Set(storyPackage.timeline.map((entry) => entry.id)),
    node: new Set(storyPackage.story.nodes.map((entry) => entry.id)),
    ending: new Set(storyPackage.story.endings.map((entry) => entry.id)),
    beat: new Set(storyPackage.story.narrativeGraph.beats.map((entry) => entry.id)),
    direction: new Set(storyPackage.directions.map((entry) => entry.id)),
  };
  const allEntityIds = new Set([...ids.character, ...ids.location, ...ids.item]);
  const addIssue = (path: (string | number)[], message: string) => context.addIssue({ code: z.ZodIssueCode.custom, path, message });
  const duplicate = (entries: string[]) => new Set(entries).size !== entries.length;
  const canReachLocation = (fromLocationId: string, toLocationId: string) => {
    const visited = new Set([fromLocationId]);
    const pending = [fromLocationId];

    while (pending.length > 0) {
      const currentLocationId = pending.shift()!;
      if (currentLocationId === toLocationId) return true;
      const location = storyPackage.locations.find((entry) => entry.id === currentLocationId);
      location?.exits.forEach((exit) => {
        if (!visited.has(exit)) {
          visited.add(exit);
          pending.push(exit);
        }
      });
    }

    return false;
  };

  const groups: [string, string[]][] = [
    ["character", storyPackage.characters.map((entry) => entry.id)], ["location", storyPackage.locations.map((entry) => entry.id)],
    ["item", storyPackage.items.map((entry) => entry.id)], ["timeline", storyPackage.timeline.map((entry) => entry.id)],
    ["node", storyPackage.story.nodes.map((entry) => entry.id)], ["ending", storyPackage.story.endings.map((entry) => entry.id)],
    ["beat", storyPackage.story.narrativeGraph.beats.map((entry) => entry.id)],
    ["direction", storyPackage.directions.map((entry) => entry.id)],
  ];
  groups.forEach(([kind, values]) => { if (duplicate(values)) addIssue([], `存在重复的 ${kind} ID`); });

  if (!ids.character.has(storyPackage.world.narrativeGuidelines.focalCharacterId)) addIssue(["world", "narrativeGuidelines", "focalCharacterId"], "焦点角色不存在");
  if (!ids.node.has(storyPackage.story.startNodeId)) addIssue(["story", "startNodeId"], "起始节点不存在");
  if (!ids.direction.has(storyPackage.defaultDirectionId)) addIssue(["defaultDirectionId"], "默认方向不存在");
  if (storyPackage.initialState.directionId !== storyPackage.defaultDirectionId) addIssue(["initialState", "directionId"], "初始方向必须等于默认方向");
  if (!ids.character.has(storyPackage.initialState.player.characterId)) addIssue(["initialState", "player", "characterId"], "玩家角色不存在");
  if (!ids.node.has(storyPackage.initialState.currentNodeId)) addIssue(["initialState", "currentNodeId"], "初始节点不存在");
  if (!ids.location.has(storyPackage.initialState.currentLocationId)) addIssue(["initialState", "currentLocationId"], "初始地点不存在");

  storyPackage.characters.forEach((character, index) => { if (!ids.location.has(character.initialLocationId)) addIssue(["characters", index, "initialLocationId"], "角色初始地点不存在"); });
  storyPackage.locations.forEach((location, index) => {
    location.exits.forEach((exit) => { if (!ids.location.has(exit)) addIssue(["locations", index, "exits"], "地点出口不存在"); });
    location.discoverableItemIds.forEach((item) => { if (!ids.item.has(item)) addIssue(["locations", index, "discoverableItemIds"], "可发现物品不存在"); });
  });
  storyPackage.items.forEach((item, index) => {
    if (item.initialHolderId && !ids.character.has(item.initialHolderId)) addIssue(["items", index, "initialHolderId"], "物品持有者不存在");
    if (item.initialLocationId && !ids.location.has(item.initialLocationId)) addIssue(["items", index, "initialLocationId"], "物品初始地点不存在");
  });
  storyPackage.story.nodes.forEach((node, index) => {
    node.transitions.forEach((transition) => { if (!ids.node.has(transition.toNodeId)) addIssue(["story", "nodes", index, "transitions"], "节点转移目标不存在"); });
    node.contextRefs.forEach((reference) => { if (!allEntityIds.has(reference)) addIssue(["story", "nodes", index, "contextRefs"], "节点上下文引用不存在"); });
  });
  if (!ids.beat.has(storyPackage.story.narrativeGraph.startBeatId)) addIssue(["story", "narrativeGraph", "startBeatId"], "叙事图起始锚点不存在");
  storyPackage.story.narrativeGraph.beats.forEach((beat, index) => {
    if (!ids.node.has(beat.nodeId)) addIssue(["story", "narrativeGraph", "beats", index, "nodeId"], "叙事锚点节点不存在");
    if (duplicate(beat.nextDirections.map((direction) => direction.id))) {
      addIssue(["story", "narrativeGraph", "beats", index, "nextDirections"], "叙事锚点存在重复的后续方向 ID");
    }
  });
  storyPackage.story.narrativeGraph.edges.forEach((edge, index) => {
    if (!ids.beat.has(edge.fromBeatId)) addIssue(["story", "narrativeGraph", "edges", index, "fromBeatId"], "叙事边起点不存在");
    if (!ids.beat.has(edge.toBeatId)) addIssue(["story", "narrativeGraph", "edges", index, "toBeatId"], "叙事边终点不存在");
  });
  const routeKeys = new Set<string>();
  storyPackage.story.sceneRoutes.forEach((route, index) => {
    const key = `${route.fromNodeId}:${route.fromLocationId}:${route.toLocationId}`;
    if (routeKeys.has(key)) addIssue(["story", "sceneRoutes", index], "场景路线重复");
    routeKeys.add(key);
    if (!ids.node.has(route.fromNodeId)) addIssue(["story", "sceneRoutes", index, "fromNodeId"], "场景路线起始节点不存在");
    if (!ids.node.has(route.toNodeId)) addIssue(["story", "sceneRoutes", index, "toNodeId"], "场景路线目标节点不存在");
    if (!ids.location.has(route.fromLocationId)) addIssue(["story", "sceneRoutes", index, "fromLocationId"], "场景路线起始地点不存在");
    if (!ids.location.has(route.toLocationId)) addIssue(["story", "sceneRoutes", index, "toLocationId"], "场景路线目标地点不存在");
    const fromNode = storyPackage.story.nodes.find((node) => node.id === route.fromNodeId);
    const toNode = storyPackage.story.nodes.find((node) => node.id === route.toNodeId);
    if (fromNode && !fromNode.contextRefs.includes(route.fromLocationId)) addIssue(["story", "sceneRoutes", index, "fromLocationId"], "场景路线起始地点不属于起始节点");
    if (toNode && !toNode.contextRefs.includes(route.toLocationId)) addIssue(["story", "sceneRoutes", index, "toLocationId"], "场景路线目标地点不属于目标节点");
    if (!canReachLocation(route.fromLocationId, route.toLocationId)) addIssue(["story", "sceneRoutes", index, "toLocationId"], "场景路线目标地点不可达");
  });
  const rejoinTargetIds = new Set<string>();
  storyPackage.story.rejoinTargets.forEach((target, index) => {
    if (rejoinTargetIds.has(target.id)) addIssue(["story", "rejoinTargets", index, "id"], "汇合目标 ID 重复");
    rejoinTargetIds.add(target.id);
    if (!ids.node.has(target.fromNodeId)) addIssue(["story", "rejoinTargets", index, "fromNodeId"], "汇合来源场景不存在");
    const targetBeat = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === target.targetBeatId);
    if (!targetBeat) addIssue(["story", "rejoinTargets", index, "targetBeatId"], "汇合目标锚点不存在");
    if (duplicate(target.requiredOpenThreads)) addIssue(["story", "rejoinTargets", index, "requiredOpenThreads"], "汇合必要线索重复");
  });
  storyPackage.story.narrativeGraph.beats.forEach((beat, beatIndex) => {
    beat.nextDirections.forEach((direction, directionIndex) => {
      if (direction.rejoinTargetId && !rejoinTargetIds.has(direction.rejoinTargetId)) {
        addIssue(["story", "narrativeGraph", "beats", beatIndex, "nextDirections", directionIndex, "rejoinTargetId"], "方向引用的汇合目标不存在");
      }
    });
  });
  Object.entries(storyPackage.story.narrativeGraph.endingBeatIds).forEach(([endingId, beatId]) => {
    if (!ids.ending.has(endingId)) addIssue(["story", "narrativeGraph", "endingBeatIds", endingId], "叙事结局不存在");
    if (!ids.beat.has(beatId)) addIssue(["story", "narrativeGraph", "endingBeatIds", endingId], "结局叙事锚点不存在");
  });
  storyPackage.directions.forEach((direction, index) => {
    direction.preferredNodeIds.forEach((node) => { if (!ids.node.has(node)) addIssue(["directions", index, "preferredNodeIds"], "方向节点不存在"); });
    direction.reachableEndingIds.forEach((ending) => { if (!ids.ending.has(ending)) addIssue(["directions", index, "reachableEndingIds"], "方向结局不存在"); });
  });
  storyPackage.rules.resolutions.forEach((resolution, index) => {
    if (!ids.node.has(resolution.nodeId)) addIssue(["rules", "resolutions", index, "nodeId"], "规则节点不存在");
    if (!allEntityIds.has(resolution.targetId)) addIssue(["rules", "resolutions", index, "targetId"], "规则目标不存在");
    if (!storyPackage.rules.actionTypes.includes(resolution.actionType)) addIssue(["rules", "resolutions", index, "actionType"], "规则行动类型未启用");
    if (!storyPackage.rules.attributes.includes(resolution.attribute)) addIssue(["rules", "resolutions", index, "attribute"], "规则属性未启用");
  });
  storyPackage.initialState.inventory.forEach((item) => { if (!ids.item.has(item)) addIssue(["initialState", "inventory"], "初始库存物品不存在"); });
  storyPackage.initialState.knownFacts.forEach((fact) => { if (!ids.timeline.has(fact)) addIssue(["initialState", "knownFacts"], "初始已知时间线不存在"); });
  Object.keys(storyPackage.initialState.relationships).forEach((character) => { if (!ids.character.has(character)) addIssue(["initialState", "relationships"], "初始关系角色不存在"); });
});

export type StoryPackage = z.infer<typeof storyPackageSchema>;
export type GameState = z.infer<typeof gameStateSchema>;

export function parseStoryPackage(input: unknown): StoryPackage {
  return storyPackageSchema.parse(input);
}

export function parseGameState(input: unknown): GameState {
  return gameStateSchema.parse(input);
}

export async function loadStoryPackage(filePath: string): Promise<StoryPackage> {
  const source = await readFile(filePath, "utf8");
  return parseStoryPackage(JSON.parse(source) as unknown);
}
