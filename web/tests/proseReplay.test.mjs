import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { replayProse } from '../src/components/proseReplay.ts'
import { cjkCount, illustrationAnchor, readingSections } from '../src/components/readingLayout.ts'

const readers = JSON.parse(execFileSync('python3', ['-B', '-c',
  'import json; from test_support.longform import longform_cases; print(json.dumps([str(c["path"].with_name("reader.json")) for c in longform_cases()]))'],
{ cwd: new URL('../../', import.meta.url), encoding: 'utf8' }))
const novels = readers.map(path => JSON.parse(readFileSync(path, 'utf8')))

function clock() {
  let id = 0
  const pending = new Map()
  return { pending, setTimeout: fn => { pending.set(++id, fn); return id }, clearTimeout: id => pending.delete(id),
    tick: () => { const callbacks = [...pending.values()]; pending.clear(); callbacks.forEach(fn => fn()) } }
}

test('saved longform prose grows over separate frames without losing or replacing text', async () => {
  for (const novel of novels) {
    const text = novel.chapters[0].text
    const timer = clock(), controller = new AbortController(), frames = []
    const done = replayProse(text, value => frames.push(value), controller.signal, timer)
    assert.ok(frames[0].length < text.length)
    while (timer.pending.size) timer.tick()
    await done
    assert.equal(frames.at(-1), text)
    for (let index = 1; index < frames.length; index++) assert.ok(frames[index].startsWith(frames[index - 1]))
  }
})

test('leaving during replay cancels timers and cannot publish another frame', async () => {
  const timer = clock(), controller = new AbortController(), frames = []
  const done = replayProse(novels[0].chapters[0].text, value => frames.push(value), controller.signal, timer)
  controller.abort(); timer.tick(); await done
  assert.equal(frames.length, 1)
  assert.equal(timer.pending.size, 0)
  await replayProse('不会显示', () => assert.fail('cancelled replay published'), controller.signal, timer)
})

test('1000 CJK triggers a fixed middle paragraph boundary for every official longform', () => {
  for (const novel of novels) {
    const text = novel.chapters.find(chapter => cjkCount(chapter.text) >= 1000).text
    const paragraphs = readingSections(text).flatMap(section => section.paragraphs)
    const anchor = illustrationAnchor(text)
    assert.ok(anchor >= 0 && anchor < paragraphs.length - 1)
    const before = paragraphs.slice(0, anchor + 1).join('')
    const full = paragraphs.join('')
    assert.ok(before.length > full.length * .25 && before.length < full.length * .75)
    assert.equal(illustrationAnchor(text.slice(0, 999)), null)
    assert.equal(readingSections(text).flatMap(section => section.paragraphs).join(''), text.split(/\n+/).filter(p => p.trim()).join(''))
  }
  assert.equal(cjkCount('。abc123𠀀汉字'), 3)
})
