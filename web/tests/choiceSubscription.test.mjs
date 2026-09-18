import test from 'node:test'
import assert from 'node:assert/strict'
import { subscribeChoices } from '../src/components/choiceSubscription.ts'

const settle = () => new Promise(resolve => setImmediate(resolve))
function fixture({ locked = false, releaseFails = false } = {}) {
  let sequence = 0, timerId = 0, subscriber = null, errors = 0
  const requests = [], released = [], shown = [], delays = [], timers = new Map(), listeners = new Map()
  const window = {
    setTimeout: (fn, delay) => { delays.push(delay); timers.set(++timerId, fn); return timerId },
    clearTimeout: id => timers.delete(id),
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name) },
  }
  const cleanup = subscribeChoices('parent', {
    prepare: id => new Promise((resolve, reject) => requests.push({ id, resolve, reject })),
    release: async id => { released.push(id); if (releaseFails) throw Error('offline') },
    subscription: id => { subscriber = id },
    locked: () => locked,
    ready: result => shown.push(result),
    error: () => { errors++ },
  }, { window, newSubscriber: () => `lease-${++sequence}` })
  return { requests, released, shown, delays, timers, listeners, cleanup,
    subscriber: () => subscriber, errors: () => errors,
    lock: value => { locked = value },
    emit: (name, persisted = true) => listeners.get(name)?.({ persisted }),
    resolve: async (index, parent = 'parent') => {
      requests[index].resolve({ parent_branch_id: parent, revision: index }); await settle()
    },
    tick: async () => {
      const pending = [...timers.values()]; timers.clear()
      pending.forEach(fn => fn()); await settle()
    },
  }
}

test('preparing directions poll quickly and ready menus return to the normal interval', async () => {
  const f = fixture()
  await f.resolve(0)
  assert.equal(f.delays.at(-1), 2500)
  await f.tick()
  f.requests[1].resolve({ parent_branch_id: 'parent', choices: [{ status: 'generating' }] })
  await settle()
  assert.equal(f.delays.at(-1), 1000)
  await f.tick()
  f.requests[2].resolve({ parent_branch_id: 'parent', choices: [{ status: 'ready' }] })
  await settle()
  assert.equal(f.delays.at(-1), 2500)
  f.cleanup()
})

test('restored page ignores late old response and releases only the old lease', async () => {
  const f = fixture()
  f.emit('pagehide'); assert.equal(f.subscriber(), null)
  f.emit('pageshow'); assert.equal(f.subscriber(), 'lease-2')
  await f.resolve(1); await f.resolve(0)
  assert.deepEqual(f.shown.map(r => r.revision), [1])
  assert.deepEqual(f.released, ['lease-1', 'lease-1'])
  assert.equal(f.timers.size, 1)
  await f.tick()
  assert.deepEqual(f.requests.map(r => r.id), ['lease-1', 'lease-2', 'lease-2'])
  f.cleanup(); await f.resolve(2)
})

test('late old error cannot replace restored directions or create another poll chain', async () => {
  const f = fixture()
  f.emit('pagehide'); f.emit('pageshow')
  await f.resolve(1)
  f.requests[0].reject(Error('late network failure')); await settle()
  assert.equal(f.errors(), 0)
  assert.equal(f.shown.length, 1)
  assert.equal(f.timers.size, 1)
  assert.ok(f.released.every(id => id === 'lease-1'))
  f.cleanup()
})

test('cleanup removes listeners and pending completion cannot publish or resume', async () => {
  const f = fixture()
  const queuedResume = f.listeners.get('pageshow')
  f.cleanup(); f.cleanup(); queuedResume({ persisted: true })
  await f.resolve(0)
  assert.equal(f.listeners.size, 0)
  assert.equal(f.requests.length, 1)
  assert.equal(f.shown.length, 0)
  assert.equal(f.timers.size, 0)
  assert.equal(f.subscriber(), null)
  assert.deepEqual(f.released, ['lease-1', 'lease-1'])
})

test('repeated pageshow starts once and non-persisted pageshow never resumes', async () => {
  const f = fixture()
  f.emit('pageshow'); f.emit('pageshow')
  assert.equal(f.requests.length, 1)
  f.emit('pagehide'); f.emit('pagehide'); f.emit('pageshow', false)
  assert.equal(f.requests.length, 1)
  f.emit('pageshow'); f.emit('pageshow')
  assert.equal(f.requests.length, 2)
  await f.resolve(0); await f.resolve(1)
  assert.equal(f.timers.size, 1)
  f.cleanup()
})

test('a timer already queued before pagehide cannot renew the released lease', async () => {
  const f = fixture()
  await f.resolve(0)
  const queuedRefresh = [...f.timers.values()][0]
  f.emit('pagehide'); queuedRefresh(); await settle()
  assert.equal(f.timers.size, 0)
  assert.equal(f.requests.length, 1)
  f.cleanup()
})

test('foreground submission pauses renewal and suppresses an in-flight menu response', async () => {
  const f = fixture({ locked: true })
  assert.equal(f.requests.length, 0)
  await f.tick(); assert.equal(f.requests.length, 0)
  f.lock(false); await f.tick(); assert.equal(f.requests.length, 1)
  f.lock(true); await f.resolve(0)
  assert.equal(f.shown.length, 0)
  await f.tick(); assert.equal(f.requests.length, 1)
  f.lock(false); await f.tick(); await f.resolve(1)
  assert.equal(f.shown.length, 1)
  f.cleanup()
})

test('wrong-parent response stays hidden and a subsequent valid response recovers', async () => {
  const f = fixture()
  await f.resolve(0, 'another-parent')
  assert.equal(f.errors(), 1); assert.equal(f.shown.length, 0)
  await f.tick(); await f.resolve(1)
  assert.equal(f.shown.length, 1)
  assert.equal(f.timers.size, 1)
  f.cleanup()
})

test('failed release does not prevent restoration and multiple old completions stay isolated', async () => {
  const f = fixture({ releaseFails: true })
  f.emit('pagehide'); f.emit('pageshow'); f.emit('pagehide'); f.emit('pageshow')
  await f.resolve(2); await f.resolve(0); await f.resolve(1)
  assert.deepEqual(f.shown.map(r => r.revision), [2])
  assert.ok(f.released.every(id => id !== 'lease-3'))
  assert.equal(f.timers.size, 1)
  assert.equal(f.errors(), 0)
  f.cleanup(); await settle()
})
