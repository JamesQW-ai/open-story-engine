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
