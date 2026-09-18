import test from 'node:test'
import assert from 'node:assert/strict'
import { observeReadingDisplay } from '../src/components/readingDisplay.ts'

function fixture(report) {
  let callback, next = 0, disconnected = false
  const frames = new Map(), timers = new Map(), listeners = new Map()
  const paragraph = { isConnected: true, getBoundingClientRect: () => ({ top: 50, left: 5, bottom: 70, right: 90, width: 85, height: 20 }) }
  const root = { isConnected: true, querySelectorAll: () => [paragraph] }
  const document = { visibilityState: 'visible', addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
  const window = { innerHeight: 800, innerWidth: 1000,
    requestAnimationFrame: fn => { frames.set(++next, fn); return next }, cancelAnimationFrame: id => frames.delete(id),
    setTimeout: fn => { timers.set(++next, fn); return next }, clearTimeout: id => timers.delete(id) }
  class IntersectionObserver {
    constructor(fn) { callback = fn }
    observe() {}
    disconnect() { disconnected = true }
  }
  const cleanup = observeReadingDisplay(root, report, { document, window, IntersectionObserver })
  const flush = async collection => {
    const scheduled = [...collection.values()]; collection.clear()
    scheduled.forEach(fn => fn()); await new Promise(resolve => setImmediate(resolve))
  }
  return { document, root, paragraph, cleanup, disconnected: () => disconnected,
    intersect: (yes = true) => callback([{ target: paragraph, isIntersecting: yes }]),
    visibility: state => { document.visibilityState = state; listeners.get('visibilitychange')?.() },
    frame: () => flush(frames), retry: () => flush(timers) }
}

test('visible paragraph reports once after paint, not on intersection alone', async () => {
  let calls = 0
  const f = fixture(async () => { calls++ })
  f.intersect(); assert.equal(calls, 0)
  await f.frame(); assert.equal(calls, 0)
  await f.frame(); assert.equal(calls, 1)
  f.intersect(); f.visibility('visible'); await f.frame(); await f.frame()
  assert.equal(calls, 1); f.cleanup(); assert.equal(f.disconnected(), true)
})

test('hidden page waits until visible and rechecks visibility before reporting', async () => {
  let calls = 0
  const f = fixture(async () => { calls++ })
  f.visibility('hidden'); f.intersect(); await f.frame(); await f.frame()
  assert.equal(calls, 0)
  f.visibility('visible'); await f.frame(); f.visibility('hidden'); await f.frame()
  assert.equal(calls, 0)
  f.visibility('visible'); await f.frame(); await f.frame()
  assert.equal(calls, 1); f.cleanup()
})

test('branch switch or a paragraph leaving the viewport cancels pending receipt', async () => {
  for (const action of ['cleanup', 'outside', 'detached']) {
    let calls = 0
    const f = fixture(async () => { calls++ })
    f.intersect(); await f.frame()
    if (action === 'cleanup') f.cleanup()
    if (action === 'outside') f.paragraph.getBoundingClientRect = () => ({ top: 900, bottom: 920, left: 5, right: 90, width: 85, height: 20 })
    if (action === 'detached') f.root.isConnected = false
    await f.frame(); assert.equal(calls, 0); f.cleanup()
  }
})

test('receipt errors retry once without affecting reading, cleanup cancels retry', async () => {
  let calls = 0
  const f = fixture(async () => { calls++; throw Error('network unavailable') })
  f.intersect(); await f.frame(); await f.frame()
  await f.retry(); await f.frame(); await f.frame()
  await f.retry(); f.visibility('visible'); await f.frame(); await f.frame()
  assert.equal(calls, 2); f.cleanup()
  const g = fixture(async () => { calls++; throw Error('network unavailable') })
  g.intersect(); await g.frame(); await g.frame(); g.cleanup()
  await g.retry(); await g.frame(); await g.frame()
  assert.equal(calls, 3)
})
