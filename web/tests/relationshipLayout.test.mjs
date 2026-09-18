import test from 'node:test'
import assert from 'node:assert/strict'
import { relationshipLayout } from '../src/components/relationshipLayout.ts'

test('graph keeps player central and never renders edges to hidden people', () => {
  const people = [{ id: 'b', first_page: 2, is_player: false }, { id: 'a', first_page: 1, is_player: true }]
  const edges = [{ source: 'a', target: 'b' }, { source: 'b', target: 'hidden' }, { source: 'a', target: 'a' }]
  const layout = relationshipLayout(people, edges)
  assert.equal(layout.nodes[0].id, 'a')
  assert.deepEqual([layout.nodes[0].x, layout.nodes[0].y], [210, 205])
  assert.deepEqual(layout.edges, [edges[0]])
  assert.deepEqual(relationshipLayout([...people].reverse(), edges), layout)
  assert.deepEqual(relationshipLayout([], edges), { nodes: [], edges: [] })
})

test('graph spreads dense casts across balanced rings', () => {
  const people = Array.from({ length: 25 }, (_, i) => ({
    id: `person-${i}`,
    first_page: i + 1,
    is_player: i === 0,
  }))
  const { nodes } = relationshipLayout(people, [])
  assert.deepEqual([nodes[0].x, nodes[0].y], [210, 205])
  assert.equal(new Set(nodes.slice(1).map(({ x, y }) => `${x}:${y}`)).size, 24)
  assert.ok(nodes.slice(1).every(({ x, y }) => x >= 60 && x <= 360 && y >= 55 && y <= 355))
  const radii = new Set(nodes.slice(1).map(({ x, y }) => Math.round(Math.hypot(x - 210, y - 205))))
  assert.deepEqual([...radii].sort((a, b) => a - b), [95, 120, 145])
})
