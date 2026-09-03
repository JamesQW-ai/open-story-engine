import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadStoryPackage } from "../content/story-package.js";

const defaultPackagePath = fileURLToPath(new URL("../../content/packages/rainy-waiting-room/0.1.0.json", import.meta.url));
const packagePath = resolve(process.argv[2] ?? defaultPackagePath);

try {
  const storyPackage = await loadStoryPackage(packagePath);
  const direction = storyPackage.directions.find((entry) => entry.id === storyPackage.defaultDirectionId);
  console.log(`Valid StoryPackage: ${storyPackage.id}@${storyPackage.version}`);
  console.log(`Canonical path: ${direction?.preferredNodeIds.join(" -> ")}`);
} catch (error) {
  console.error(`StoryPackage validation failed: ${packagePath}`);
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
}
