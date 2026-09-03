import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parseStoryPackage } from "../src/content/story-package.js";

const packagePath = fileURLToPath(new URL("../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const sourcePath = fileURLToPath(new URL("../content/source/rainy-waiting-room.v0.1.txt", import.meta.url));

async function readCandidatePackage(): Promise<unknown> {
  return JSON.parse(await readFile(packagePath, "utf8")) as unknown;
}

describe("StoryPackage schema", () => {
  it("accepts the rainy-waiting-room candidate package", async () => {
    const storyPackage = parseStoryPackage(await readCandidatePackage());
    expect(storyPackage.defaultDirectionId).toBe("direction_original_canon");
    expect(storyPackage.story.nodes.map((node) => node.id)).toEqual(["node_arrival", "node_records", "node_tunnel", "node_aftermath"]);
  });

  it("keeps the co-creation opening as a traceable source excerpt instead of a generated anchor", async () => {
    const storyPackage = parseStoryPackage(await readCandidatePackage());
    const opening = storyPackage.story.narrativeGraph.beats.find((beat) => beat.id === storyPackage.story.narrativeGraph.startBeatId);
    const sourceLines = (await readFile(sourcePath, "utf8")).split("\n");

    expect(opening?.sourceExcerpt).toMatchObject({
      lineRange: [5, 54],
    });
    expect(opening?.sourceExcerpt?.text).toBe(sourceLines.slice(4, 54).join("\n"));
    expect(opening?.narrativeAnchor).toBe("候车厅的灯光在雨幕里忽明忽暗，唐栖那段没能说完的语音仍在许川耳边。");
  });

  it("keeps every canonical excerpt continuous and traceable to the source novel", async () => {
    const storyPackage = parseStoryPackage(await readCandidatePackage());
    const sourceLines = (await readFile(sourcePath, "utf8")).split("\n");
    const canonicalBeatIds = [
      "beat_arrival",
      "beat_token_found",
      "beat_office_entered",
      "beat_evidence_secured",
      "beat_tunnel_with_proof",
      "beat_valve_with_proof",
      "beat_aftermath",
      "beat_ending_truth_survives",
    ];
    let previousEnd = 4;

    for (const beatId of canonicalBeatIds) {
      const beat = storyPackage.story.narrativeGraph.beats.find((candidate) => candidate.id === beatId);
      const excerpt = beat?.sourceExcerpt;
      expect(excerpt).toBeDefined();
      const [start, end] = excerpt!.lineRange;
      expect(start).toBe(previousEnd + 1);
      expect(excerpt!.text).toBe(sourceLines.slice(start - 1, end).join("\n"));
      previousEnd = end;
    }
    expect(previousEnd).toBe(417);
  });

  it("requires every published story direction to declare a state patch", async () => {
    const storyPackage = parseStoryPackage(await readCandidatePackage());
    for (const beat of storyPackage.story.narrativeGraph.beats) {
      for (const direction of beat.nextDirections) {
        expect(Object.keys(direction.statePatch)).not.toHaveLength(0);
      }
    }
  });

  it("rejects a package whose default direction does not exist", async () => {
    const candidate = await readCandidatePackage() as { defaultDirectionId: string };
    candidate.defaultDirectionId = "direction_missing";
    expect(() => parseStoryPackage(candidate)).toThrow("默认方向不存在");
  });
});
