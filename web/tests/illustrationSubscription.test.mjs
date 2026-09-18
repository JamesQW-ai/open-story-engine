import test from 'node:test'
import assert from 'node:assert/strict'
import { subscribeIllustration } from '../src/components/illustrationSubscription.ts'

const settle = () => new Promise(resolve => setImmediate(resolve))
function fixture() {
  let id = 0, serial = 0, subscriber = null, errors = 0
  const requests = [], shown = [], releases = [], timers = new Map(), listeners = new Map()
  const controller = subscribeIllustration({
    prepare: (id, draw) => new Promise((resolve, reject) => requests.push({ id, draw, resolve, reject })),
    release: async id => { releases.push(id) },
    subscription: value => { subscriber = value },
    ready: result => shown.push(result),
    error: () => { errors++ },
  }, { newSubscriber: () => `image-${++serial}`, window: {
    setTimeout: fn => { timers.set(++id, fn); return id }, clearTimeout: id => timers.delete(id),
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name),
  } })
  return { ...controller, requests, shown, releases, timers, listeners, subscriber: () => subscriber, errors: () => errors,
    emit: name => listeners.get(name)?.({ persisted: true }),
    resolve: async (i, status = 'ready', can_generate = false) => {
      requests[i].resolve({ items: [{ status }], can_generate, revision: i }); await settle()
    },
    tick: async () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()); await settle() },
  }
}

test('image polling stops at terminal state and loading alone never requests drawing', async () => {
  const f = fixture()
  assert.equal(f.requests[0].draw, false)
  await f.resolve(0, 'generating'); await f.tick()
  assert.equal(f.requests[1].draw, false)
  await f.resolve(1)
  assert.equal(f.timers.size, 0); f.cleanup()
})

test('draw during pending lookup is serialized and repeated clicks spend once', async () => {
  const f = fixture()
  f.draw(); f.draw(); assert.equal(f.requests.length, 1)
  await f.resolve(0, 'idle', true); await f.tick()
  assert.equal(f.requests.length, 2); assert.equal(f.requests[1].draw, true)
  f.draw(); await f.resolve(1, 'generating'); await f.tick(); await f.resolve(2)
  assert.equal(f.requests.filter(r => r.draw).length, 1)
  assert.equal(f.timers.size, 0); f.cleanup()
})

test('image page restoration isolates late responses and never repeats a draw request', async () => {
  const f = fixture()
  await f.resolve(0, 'idle', true); f.draw()
  f.emit('pagehide'); f.emit('pageshow'); f.emit('pageshow')
  assert.equal(f.requests.length, 3)
  assert.notEqual(f.requests[1].id, f.requests[2].id)
  assert.equal(f.requests[2].draw, false)
  await f.resolve(2, 'generating'); await f.resolve(1)
  assert.deepEqual(f.shown.map(r => r.revision), [0, 2])
  assert.ok(f.releases.every(id => id === 'image-1'))
  assert.equal(f.timers.size, 1); f.cleanup()
})

test('cancel suppresses late image and stops requests without affecting another lease', async () => {
  const f = fixture()
  f.cancel(); await f.resolve(0)
  f.draw(); await f.tick()
  assert.equal(f.subscriber(), null)
  assert.equal(f.requests.length, 1); assert.equal(f.shown.length, 0)
  f.cleanup(); assert.equal(f.listeners.size, 0)
})

test('image errors retry within a fixed limit and old errors cannot schedule work', async () => {
  const f = fixture()
  for (let i = 0; i < 3; i++) {
    f.requests[i].reject(Error('offline')); await settle(); await f.tick()
  }
  assert.equal(f.requests.length, 3); assert.equal(f.timers.size, 0)
  assert.equal(f.errors(), 1)
  f.emit('pagehide'); f.emit('pageshow'); f.cleanup()
  f.requests[3].reject(Error('late')); await settle()
  assert.equal(f.timers.size, 0)
  assert.equal(f.errors(), 1)
})
