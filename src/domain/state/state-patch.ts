import { parseGameState, type GameState } from "../../content/story-package.js";

type JsonPrimitive = boolean | number | string | null;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type StatePatchOperation = {
  op: "set" | "remove";
  path: string[];
  value?: JsonValue;
};

export function createStatePatch(before: GameState, after: GameState): StatePatchOperation[] {
  const operations: StatePatchOperation[] = [];
  collectDifferences(before as JsonValue, after as JsonValue, [], operations);
  return operations;
}

export function applyStatePatch(initialState: GameState, operations: StatePatchOperation[]): GameState {
  let nextState: JsonValue = structuredClone(initialState) as JsonValue;

  for (const operation of operations) {
    if (operation.path.length === 0) {
      if (operation.op !== "set" || operation.value === undefined) throw new Error("状态根节点必须通过 set 操作替换");
      nextState = structuredClone(operation.value);
      continue;
    }

    const parent = getParent(nextState, operation.path);
    const key = operation.path.at(-1);
    if (!key) throw new Error("状态补丁缺少目标路径");

    if (operation.op === "remove") {
      if (Array.isArray(parent)) {
        const index = Number(key);
        if (!Number.isInteger(index)) throw new Error(`数组补丁索引无效: ${key}`);
        parent.splice(index, 1);
      } else {
        delete parent[key];
      }
      continue;
    }

    if (operation.value === undefined) throw new Error(`set 补丁缺少值: ${operation.path.join(".")}`);
    if (Array.isArray(parent)) {
      const index = Number(key);
      if (!Number.isInteger(index)) throw new Error(`数组补丁索引无效: ${key}`);
      parent[index] = structuredClone(operation.value);
    } else {
      parent[key] = structuredClone(operation.value);
    }
  }

  return parseGameState(nextState);
}

function collectDifferences(before: JsonValue | undefined, after: JsonValue | undefined, path: string[], operations: StatePatchOperation[]): void {
  if (after === undefined) {
    operations.push({ op: "remove", path });
    return;
  }
  if (before === undefined) {
    operations.push({ op: "set", path, value: structuredClone(after) });
    return;
  }
  if (isRecord(before) && isRecord(after)) {
    const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
    for (const key of [...keys].sort()) collectDifferences(before[key], after[key], [...path, key], operations);
    return;
  }
  if (JSON.stringify(before) !== JSON.stringify(after)) {
    operations.push({ op: "set", path, value: structuredClone(after) });
  }
}

function getParent(root: JsonValue, path: string[]): JsonValue[] | { [key: string]: JsonValue } {
  let current = root;
  for (const segment of path.slice(0, -1)) {
    if (Array.isArray(current)) {
      const index = Number(segment);
      if (!Number.isInteger(index) || current[index] === undefined) throw new Error(`状态补丁路径不存在: ${path.join(".")}`);
      current = current[index];
    } else if (isRecord(current) && current[segment] !== undefined) {
      current = current[segment];
    } else {
      throw new Error(`状态补丁路径不存在: ${path.join(".")}`);
    }
  }
  if (!Array.isArray(current) && !isRecord(current)) throw new Error(`状态补丁父级不是容器: ${path.join(".")}`);
  return current;
}

function isRecord(value: JsonValue): value is { [key: string]: JsonValue } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
