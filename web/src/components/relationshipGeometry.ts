export type GraphPoint = { x: number; y: number }

// The graph node includes the name below the portrait. Shift the relationship
// label toward that combined visual center instead of using the portrait center.
const NODE_NAME_VISUAL_OFFSET = 14

export function relationshipLabelPosition(a: GraphPoint, b: GraphPoint): GraphPoint {
  return {
    x: (a.x + b.x) / 2,
    y: (a.y + b.y) / 2 + NODE_NAME_VISUAL_OFFSET,
  }
}
