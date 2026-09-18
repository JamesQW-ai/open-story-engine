import test from 'node:test'
import assert from 'node:assert/strict'
import { createReadingTiming, saveReadingTiming, READING_TIMINGS_KEY } from '../src/components/readingTimings.ts'

function attempt(id = 'attempt-1') {
  let now = 100
  const records = []
  const meter = createReadingTiming({ attempt_id: id, request_id: 'same-request', session_id: 'session' },
    record => records.push(record), () => now)
  return { meter, records, at: ms => { now = 100 + ms }, latest: () => records.at(-1) }
}

test('opening keeps one clock across session assignment and waits for its own menu', () => {
  let now = 0
  const records = []
  const meter = createReadingTiming({ attempt_id: 'opening-1', request_id: 'start', session_id: 'new', kind: 'opening' },
    r => records.push(r), () => now)
  now = 100 // Includes initial image preparation before text.
  meter.rendered('开篇第一句', true, undefined, false)
  meter.confirm('root', 'saved-session')
  now = 240
  meter.rendered('完整开篇', false, 'root', false)
  now = 310
  meter.rendered('完整开篇', false, 'root', true)
  const result = records.at(-1)
  assert.equal(result.kind, 'opening')
  assert.equal(result.session_id, 'saved-session')
  assert.equal(result.branch_id, 'root')
  assert.equal(result.first_text_ms, 100)
  assert.equal(result.full_text_ms, 240)
  assert.equal(result.directions_available_ms, 310)
  assert.ok(records.every(r => r.attempt_id === 'opening-1'))
})

test('stream reset preserves first visible text; full prose and delayed menu have distinct clocks', () => {
  const a = attempt()
  a.at(15); a.meter.rendered('   ', true, 'old', true)
  assert.equal(a.latest().first_text_ms, null)
  a.at(20); a.meter.rendered('草稿', true, 'old', true)
  a.at(30); a.meter.rendered('', true, 'old', true)
  a.at(40); a.meter.rendered('修复后的正文', true, 'old', true)
  assert.equal(a.latest().first_text_ms, 20)
  assert.equal(a.latest().full_text_ms, null)
  assert.equal(a.latest().directions_available_ms, null)
  a.meter.confirm('new')
  a.at(60); a.meter.rendered('修复后的正文', false, 'new', false)
  assert.equal(a.latest().full_text_ms, 60)
  a.at(85); a.meter.rendered('修复后的正文', false, 'new', true)
  assert.equal(a.latest().directions_available_ms, 85)
  a.at(99); a.meter.rendered('其他历史页面', false, 'other', true)
  assert.equal(a.latest().directions_available_ms, 85)
  assert.equal(a.records.length, 4)
})

test('cache hit without deltas records first text and complete prose together', () => {
  const a = attempt()
  a.at(5); a.meter.rendered('旧正文', false, 'old', true)
  assert.equal(a.latest().first_text_ms, null)
  a.meter.confirm('cached')
  a.at(12); a.meter.rendered('缓存正文', false, 'cached', false)
  assert.equal(a.latest().first_text_ms, 12)
  assert.equal(a.latest().full_text_ms, 12)
  assert.equal(a.latest().directions_available_ms, null)
  assert.equal(a.latest().outcome, 'completed')
})

test('failed draft and restored page never count as complete; retry is a separate attempt', () => {
  const a = attempt()
  a.at(8); a.meter.rendered('尚未确认', true, 'old', false)
  a.meter.finish('failed')
  a.at(15); a.meter.rendered('上一页', false, 'old', true)
  assert.equal(a.latest().outcome, 'failed')
  assert.equal(a.latest().full_text_ms, null)
  const retry = attempt('attempt-2')
  retry.meter.confirm('recovered')
  retry.at(3); retry.meter.rendered('已恢复', false, 'recovered', true)
  assert.equal(retry.latest().request_id, a.latest().request_id)
  assert.notEqual(retry.latest().attempt_id, a.latest().attempt_id)
  assert.equal(retry.latest().first_text_ms, 3)
})

test('interruption stays incomplete; ending without choices keeps menu unavailable', () => {
  const a = attempt()
  a.meter.finish('interrupted')
  a.meter.confirm('late')
  a.meter.rendered('迟到结果', false, 'late', true)
  assert.equal(a.latest().outcome, 'interrupted')
  assert.equal(a.latest().full_text_ms, null)
  const ending = attempt()
  ending.meter.confirm('end')
  ending.meter.rendered('终局正文', false, 'end', false)
  ending.meter.finish('interrupted')
  assert.equal(ending.latest().outcome, 'completed')
  assert.equal(ending.latest().directions_available_ms, null)
})

test('diagnostics retain only 50 attempts and update in place without storing prose', () => {
  const data = new Map()
  const storage = { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) }
  for (let i = 0; i < 55; i++) saveReadingTiming(attempt(`attempt-${i}`).latest(), storage)
  let saved = JSON.parse(data.get(READING_TIMINGS_KEY))
  assert.equal(saved.length, 50)
  assert.equal(saved[0].attempt_id, 'attempt-5')
  const last = attempt('attempt-54')
  last.meter.confirm('result'); last.meter.rendered('不应进入诊断的正文', false, 'result', true)
  saveReadingTiming(last.latest(), storage)
  saved = JSON.parse(data.get(READING_TIMINGS_KEY))
  assert.equal(saved.length, 50)
  assert.equal(saved.at(-1).outcome, 'completed')
  assert.ok(!data.get(READING_TIMINGS_KEY).includes('不应进入'))
  assert.doesNotThrow(() => saveReadingTiming(last.latest(), { getItem() { throw Error('denied') }, setItem() {} }))
})
