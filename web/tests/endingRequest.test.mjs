import test from 'node:test'
import assert from 'node:assert/strict'
import { endingRequest, closureActions } from '../src/components/endingRequest.ts'

function memory() {
  const data = new Map()
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) }
}
test('lost review response and reload preserve identity; explicit new review gets a new id', () => {
  const storage = memory()
  let id = 0
  const factory = () => String(++id)
  const first = endingRequest('session:branch', storage, factory)
  const attempt = first.prepare('结局说明', '正文引用')
  assert.deepEqual(first.prepare('结局说明', '正文引用'), attempt)
  const restored = endingRequest('session:branch', storage, factory)
  assert.deepEqual(restored.saved(), attempt)
  assert.deepEqual(restored.prepare('结局说明', '正文引用'), attempt)
  restored.clear()
  assert.notEqual(restored.prepare('结局说明', '正文引用').request_id, attempt.request_id)
})
test('branches isolate requests and editing evidence allocates a different identity', () => {
  const storage = memory()
  let id = 0
  const factory = () => String(++id)
  const first = endingRequest('a', storage, factory)
  const old = first.prepare('说明', '引用')
  assert.equal(endingRequest('b', storage, factory).saved(), null)
  assert.notEqual(first.prepare('新说明', '引用').request_id, old.request_id)
})
test('corrupt storage is ignored and persistence failure prevents a sendable attempt', () => {
  const storage = memory()
  storage.setItem('a', '{broken')
  assert.equal(endingRequest('a', storage).saved(), null)
  storage.setItem = () => { throw Error('storage unavailable') }
  const req = endingRequest('a', storage, () => 'stable')
  assert.throws(() => req.prepare('说明', '引用'), /storage unavailable/)
  assert.throws(() => req.prepare('说明', '引用'), /storage unavailable/)
})
test('double clicks serialize; disposing suppresses late result and parent refresh', async () => {
  const shown = [], busy = [], errors = []
  let resolve, calls = 0
  const controller = closureActions({ busy: value => busy.push(value), error: e => errors.push(e) })
  const running = controller.run(() => { calls++; return new Promise(r => { resolve = r }) }, value => shown.push(value))
  await controller.run(async () => { calls++; return 'duplicate' }, value => shown.push(value))
  assert.equal(calls, 1)
  controller.dispose()
  resolve('old branch result'); await running
  assert.deepEqual(shown, [])
  assert.deepEqual(busy, [true])
  assert.deepEqual(errors, [])
  await controller.run(async () => { calls++ }, () => {})
  assert.equal(calls, 1)
})
test('failure releases action lock and does not automatically retry', async () => {
  const shown = [], busy = [], errors = []
  let calls = 0
  const controller = closureActions({ busy: value => busy.push(value), error: e => errors.push(e.message) })
  await controller.run(async () => { calls++; throw Error('network') }, value => shown.push(value))
  assert.equal(calls, 1)
  assert.deepEqual(errors, ['network'])
  await controller.run(async () => { calls++; return 'recovered' }, value => shown.push(value))
  assert.deepEqual(shown, ['recovered'])
  assert.deepEqual(busy, [true, false, true, false])
})
