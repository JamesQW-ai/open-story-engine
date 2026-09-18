import test from 'node:test'
import assert from 'node:assert/strict'
import { relationshipLabelPosition } from '../src/components/relationshipGeometry.ts'

test('relationship label accounts for the name below each portrait', () => {
  const a = { x: 210, y: 60 }
  const b = { x: 210, y: 350 }
  assert.deepEqual(relationshipLabelPosition(a, b), { x: 210, y: 219 })
})

test('relationship label keeps the name offset for diagonal connections', () => {
  const a = { x: 100, y: 100 }
  const b = { x: 300, y: 240 }
  const label = relationshipLabelPosition(a, b)
  assert.equal(label.x, 200)
  assert.equal(label.y, 184)
})
