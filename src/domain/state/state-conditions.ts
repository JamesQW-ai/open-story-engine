import type { GameState } from "../../content/story-package.js";

export function matchesStateCondition(state: GameState, condition: string): boolean {
  if (condition.startsWith("has:")) return state.inventory.includes(condition.slice("has:".length));
  if (condition.startsWith("not_has:")) return !state.inventory.includes(condition.slice("not_has:".length));
  if (condition.startsWith("!")) return !state.flags[condition.slice(1)];

  const comparison = condition.match(/^([A-Za-z0-9_]+)(>=|<=|=|>|<)(-?\d+)$/);
  if (comparison) {
    const [, field, operator, rawValue] = comparison;
    const currentValue = state.counters[field ?? ""];
    const expectedValue = Number(rawValue);
    if (currentValue === undefined) return false;
    switch (operator) {
      case ">=": return currentValue >= expectedValue;
      case "<=": return currentValue <= expectedValue;
      case ">": return currentValue > expectedValue;
      case "<": return currentValue < expectedValue;
      case "=": return currentValue === expectedValue;
      default: return false;
    }
  }
  return state.flags[condition] === true;
}
