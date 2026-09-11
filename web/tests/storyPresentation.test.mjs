import test from 'node:test'
import assert from 'node:assert/strict'
import { prologueText, storyChoices, identityEntry } from '../src/components/storyPresentation.ts'

test('other novels select the earliest declared entry led by the chosen character', () => {
  const catalog = {package:{title:'灯塔'},characters:[{id:'lin',name:'林舟'},{id:'he',name:'何雨'}],entries:[
    {id:'shared',summary:'林舟来到灯塔。',chapter_id:'chapter-001',beat_id:'beat_001',source_character_ids:['lin','he']},
    {id:'late',summary:'何雨离开灯塔。',chapter_id:'chapter-003',beat_id:'beat_004',source_character_ids:['he']},
    {id:'early',summary:'何雨检查电台。',chapter_id:'chapter-002',beat_id:'beat_003',source_character_ids:['he']},
  ]}
  assert.equal(identityEntry(catalog,'lin').id,'shared')
  assert.equal(identityEntry(catalog,'he').id,'early')
  assert.equal(identityEntry(catalog,'missing').id,'shared')
})

test('prologue stays within 50 Unicode characters', () => {
  for (const title of ['雨夜候车室', '另一部小说']) {
    const text = prologueText({package:{title, summary:'雨'.repeat(100)}})
    assert.ok([...text].length <= 50)
    assert.ok(text.length > 0)
  }
})

test('all menus offer at least two distinct executable choices without invented IDs', () => {
  for (const nextDirections of [[], [{id:'placeholder',title:'继续当前目标'}], [{id:'real',title:'询问陈砚',summary:'询问零点前放行的原因。'}]]) {
    const choices = storyChoices({nextDirections}, '候车厅')
    assert.ok(choices.length >= 2)
    assert.equal(new Set(choices.map(c=>c.title)).size, choices.length)
    for (const c of choices) {
      assert.ok(c.summary.length >= 10)
      assert.ok(c.payload.text || nextDirections.some(d=>d.id === c.payload.direction_id))
      assert.notEqual(c.title,'继续当前目标')
    }
  }
})
