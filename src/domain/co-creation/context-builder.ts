import { z } from "zod";
import type { StoryPackage } from "../../content/story-package.js";
import type { BranchNode } from "./branch-node.js";
import { branchStateSchema } from "./branch-state.js";
import type { SessionStoryContract } from "./session-story-contract.js";

const contextReferenceSchema = z.object({
  kind: z.enum(["immutable_fact", "canonical_node", "branch_node"]),
  ref: z.string().min(1),
  summary: z.string().min(1),
});

export const coCreationContextSchema = z.object({
  contract: z.object({
    entryNodeId: z.string().min(1),
    canonicalPrefixNodeIds: z.array(z.string().min(1)).min(1),
    continuityScope: z.enum(["world_only", "world_and_characters", "canonical_until_entry"]),
    personaName: z.string().min(1),
    direction: z.object({ id: z.string().min(1), title: z.string().min(1), summary: z.string().min(1) }),
  }),
  parent: z.object({
    id: z.string().min(1),
    sourceNodeRef: z.string().min(1).optional(),
    summary: z.string().min(1),
    openThreads: z.array(z.string().min(1)),
    canonicalRelation: z.enum(["on_line", "diverged", "rejoined"]),
    branchState: branchStateSchema,
    nextDirections: z.array(z.object({ id: z.string().min(1), title: z.string().min(1), summary: z.string().min(1), canonicalBeatId: z.string().min(1).optional(), sourceNodeRef: z.string().min(1).optional() })),
  }),
  sourceWindow: z.object({
    nodeId: z.string().min(1),
    objective: z.string().min(1),
    sceneSetup: z.array(z.string().min(1)).min(1),
    entityContext: z.array(z.object({ id: z.string().min(1), name: z.string().min(1), summary: z.string().min(1) })),
  }),
  branchLedger: z.array(z.object({ id: z.string().min(1), summary: z.string().min(1), canonicalRelation: z.enum(["on_line", "diverged", "rejoined"]) })).min(1),
  narrativeConstraints: z.object({
    perspective: z.literal("third_person_limited"),
    focalCharacterId: z.string().min(1),
    prohibitions: z.array(z.string().min(1)).min(1),
    globalConstraints: z.array(z.string().min(1)).min(1),
  }),
  availableReferences: z.array(contextReferenceSchema).min(1),
});

export type CoCreationContext = z.infer<typeof coCreationContextSchema>;

type BranchLineageNode = BranchNode & { id: string };

export class CoCreationContextBuilder {
  build(storyPackage: StoryPackage, contract: SessionStoryContract, lineage: BranchLineageNode[], sourceNodeRef?: string): CoCreationContext {
    const parent = lineage.at(-1);
    if (!parent) throw new Error("共创上下文缺少父分支节点");
    const nodeId = sourceNodeRef ?? parent.sourceNodeRef ?? contract.entryNodeId;
    const sourceNode = storyPackage.story.nodes.find((node) => node.id === nodeId);
    if (!sourceNode) throw new Error(`共创上下文引用的原著节点不存在: ${nodeId}`);

    const immutableReferences = storyPackage.world.immutableFacts
      .filter((fact) => contract.immutableFactRefs.includes(fact.id))
      .map((fact) => ({ kind: "immutable_fact" as const, ref: fact.id, summary: fact.text }));
    const canonicalReferences = contract.canonicalPrefixNodeIds.map((canonicalNodeId) => {
      const canonicalNode = storyPackage.story.nodes.find((node) => node.id === canonicalNodeId);
      if (!canonicalNode) throw new Error(`共创规范前史节点不存在: ${canonicalNodeId}`);
      return { kind: "canonical_node" as const, ref: canonicalNode.id, summary: canonicalNode.objective };
    });
    const branchReferences = lineage.map((node) => ({ kind: "branch_node" as const, ref: node.id, summary: node.summary }));

    return coCreationContextSchema.parse({
      contract: {
        entryNodeId: contract.entryNodeId,
        canonicalPrefixNodeIds: contract.canonicalPrefixNodeIds,
        continuityScope: contract.continuityScope,
        personaName: contract.persona.name,
        direction: contract.direction,
      },
      parent: {
        id: parent.id,
        sourceNodeRef: parent.sourceNodeRef,
        summary: parent.summary,
        openThreads: parent.openThreads,
        canonicalRelation: parent.canonicalRelation,
        branchState: parent.branchState,
        nextDirections: parent.nextDirections,
      },
      sourceWindow: {
        nodeId: sourceNode.id,
        objective: sourceNode.objective,
        sceneSetup: sourceNode.sceneSetup,
        entityContext: sourceNode.contextRefs.map((reference) => describeEntity(storyPackage, reference)),
      },
      branchLedger: lineage.map((node) => ({ id: node.id, summary: node.summary, canonicalRelation: node.canonicalRelation })),
      narrativeConstraints: {
        perspective: storyPackage.world.narrativeGuidelines.perspective,
        focalCharacterId: storyPackage.world.narrativeGuidelines.focalCharacterId,
        prohibitions: storyPackage.world.narrativeGuidelines.prohibitions,
        globalConstraints: storyPackage.world.globalConstraints,
      },
      availableReferences: [...immutableReferences, ...canonicalReferences, ...branchReferences],
    });
  }
}

function describeEntity(storyPackage: StoryPackage, reference: string): { id: string; name: string; summary: string } {
  const character = storyPackage.characters.find((entity) => entity.id === reference);
  if (character) return { id: character.id, name: character.name, summary: character.description };
  const item = storyPackage.items.find((entity) => entity.id === reference);
  if (item) return { id: item.id, name: item.name, summary: item.effects.join("；") };
  const location = storyPackage.locations.find((entity) => entity.id === reference);
  if (location) return { id: location.id, name: location.name, summary: location.description };
  throw new Error(`共创上下文引用的实体不存在: ${reference}`);
}
