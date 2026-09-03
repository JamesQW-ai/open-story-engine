import { randomUUID } from "node:crypto";
import { z } from "zod";
import type { StoryPackage } from "../../content/story-package.js";

const idSchema = z.string().min(1).regex(/^[A-Za-z0-9_-]+$/);

export const sessionStoryContractSchema = z.object({
  id: idSchema,
  sessionId: idSchema,
  mode: z.literal("derivative_co_creation"),
  sourcePackageRef: z.object({ id: idSchema, version: z.string().min(1) }),
  entryNodeId: idSchema,
  canonicalPrefixNodeIds: z.array(idSchema).min(1),
  canonicalTimelineRefs: z.array(idSchema),
  continuityScope: z.enum(["world_only", "world_and_characters", "canonical_until_entry"]),
  persona: z.discriminatedUnion("kind", [
    z.object({ kind: z.literal("source_character"), sourceCharacterId: idSchema, name: z.string().min(1) }),
    z.object({ kind: z.literal("new_character"), name: z.string().min(1), background: z.string().min(1), motivation: z.string().min(1) }),
  ]),
  immutableFactRefs: z.array(idSchema).min(1),
  direction: z.object({ id: idSchema, title: z.string().min(1), summary: z.string().min(1) }),
  provenance: z.array(z.object({ kind: z.enum(["source", "user", "derived"]), ref: z.string().min(1), note: z.string().min(1) })).min(1),
  createdAt: z.string().datetime(),
});

export type SessionStoryContract = z.infer<typeof sessionStoryContractSchema>;

export type CreateSessionStoryContractInput = {
  sessionId: string;
  entryNodeId?: string;
  continuityScope?: SessionStoryContract["continuityScope"];
  persona?: SessionStoryContract["persona"];
  directionId?: string;
};

export function createSessionStoryContract(
  storyPackage: StoryPackage,
  input: CreateSessionStoryContractInput,
): SessionStoryContract {
  const entryNodeId = input.entryNodeId ?? storyPackage.story.startNodeId;
  const entryNode = storyPackage.story.nodes.find((node) => node.id === entryNodeId);
  if (!entryNode) throw new Error(`共创起始节点不存在: ${entryNodeId}`);

  const direction = storyPackage.directions.find((candidate) => candidate.id === (input.directionId ?? storyPackage.defaultDirectionId));
  if (!direction) throw new Error(`共创剧情方向不存在: ${input.directionId}`);

  const persona = input.persona ?? sourcePersona(storyPackage);
  if (persona.kind === "source_character" && !storyPackage.characters.some((character) => character.id === persona.sourceCharacterId)) {
    throw new Error(`共创代入角色不存在: ${persona.sourceCharacterId}`);
  }

  const canonicalPrefixNodeIds = findCanonicalPrefix(storyPackage, entryNodeId);
  const immutableFactRefs = storyPackage.world.immutableFacts.map((fact) => fact.id);
  return sessionStoryContractSchema.parse({
    id: `contract_${randomUUID()}`,
    sessionId: input.sessionId,
    mode: "derivative_co_creation",
    sourcePackageRef: { id: storyPackage.id, version: storyPackage.version },
    entryNodeId,
    canonicalPrefixNodeIds,
    canonicalTimelineRefs: storyPackage.timeline.filter((entry) => entry.knownAtStart).map((entry) => entry.id),
    continuityScope: input.continuityScope ?? "canonical_until_entry",
    persona,
    immutableFactRefs,
    direction: { id: direction.id, title: direction.title, summary: direction.summary },
    provenance: [
      { kind: "source", ref: `${storyPackage.id}@${storyPackage.version}`, note: "固定原著故事包引用" },
      { kind: "derived", ref: entryNodeId, note: "从原著规范路径计算出的进入前史" },
    ],
    createdAt: new Date().toISOString(),
  });
}

export function parseSessionStoryContract(input: unknown): SessionStoryContract {
  return sessionStoryContractSchema.parse(input);
}

function sourcePersona(storyPackage: StoryPackage): Extract<SessionStoryContract["persona"], { kind: "source_character" }> {
  const character = storyPackage.characters.find((candidate) => candidate.id === storyPackage.initialState.player.characterId);
  if (!character) throw new Error(`初始玩家角色不存在: ${storyPackage.initialState.player.characterId}`);
  return { kind: "source_character", sourceCharacterId: character.id, name: character.name };
}

function findCanonicalPrefix(storyPackage: StoryPackage, entryNodeId: string): string[] {
  const prefix: string[] = [];
  const visited = new Set<string>();
  let nodeId: string | undefined = storyPackage.story.startNodeId;

  while (nodeId) {
    if (visited.has(nodeId)) throw new Error("原著规范路径存在循环，无法创建共创前史");
    visited.add(nodeId);
    prefix.push(nodeId);
    if (nodeId === entryNodeId) return prefix;
    const node = storyPackage.story.nodes.find((candidate) => candidate.id === nodeId);
    nodeId = node?.transitions.find((transition) => transition.canonical)?.toNodeId;
  }

  throw new Error(`共创起始节点不在规范原著路径上: ${entryNodeId}`);
}
