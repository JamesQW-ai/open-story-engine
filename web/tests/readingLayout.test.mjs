import test from 'node:test'
import assert from 'node:assert/strict'
import { readingSections } from '../src/components/readingLayout.ts'
import { storyChoices } from '../src/components/storyPresentation.ts'

test('long reading sections preserve every character in order, including unbroken prose', () => {
  for (const text of [('雨声沿着站台传来。\n\n').repeat(240), '雨'.repeat(3100), '短短的一页。']) {
    const sections = readingSections(text)
    assert.equal(sections.flatMap(s => s.paragraphs).join(''), text.replace(/\n/g, ''))
    if (text.length > 1000) assert.ok(sections.length >= 3)
    else assert.equal(sections.length, 1)
  }
})

test('streaming growth keeps completed reading sections stable', () => {
  const prefix = ('雨声落在玻璃上。\n\n').repeat(160)
  const before = readingSections(prefix)
  const after = readingSections(prefix + '新的声音从门外传来。'.repeat(150))
  assert.deepEqual(after.slice(0, before.length - 1), before.slice(0, -1))
})

test('identity opening actions submit their own prose', () => {
  const actions = [{title:'敲击铁门',summary:'停下来听是否有人回应。'}, {title:'观察设备',summary:'看看指示灯旁的设施。'}]
  const choices = storyChoices({kind:'source_entry', openingActions:actions, nextDirections:[]}, '信号室')
  assert.equal(choices.length, 2)
  assert.equal(choices[0].payload.text, '敲击铁门。停下来听是否有人回应。')
})
