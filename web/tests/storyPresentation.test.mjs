import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { prologueText, storyChoices, identityEntry } from '../src/components/storyPresentation.ts'

test('official defaults override summary heuristics and reject invalid bindings', () => {
  const catalog = {package:{title:'太虚遗录'},characters:[{id:'gu',name:'顾长离',defaultEntryPointId:'official'}],entries:[
    {id:'earlier',summary:'顾长离走来。',source_character_ids:['gu']},
    {id:'official',summary:'试炼将开始。',source_character_ids:['gu']},
  ]}
  assert.equal(identityEntry(catalog, 'gu').id, 'official')
  catalog.entries[1].source_character_ids = ['lu']
  assert.equal(identityEntry(catalog, 'gu'), undefined)
})

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

test('long novel prologue preserves its scene and fallback summary stays bounded', () => {
  const {metadata} = JSON.parse(readFileSync(new URL('../../content/packages/taixu-relics-part1/0.1.2/package.json', import.meta.url), 'utf8'))
  const text = prologueText({package:metadata})
  assert.ok(text.includes('玄霄宗的山门'))
  assert.ok(text.endsWith('还是守住门规？'))
  const fallback = prologueText({package:{...metadata, title:''}})
  assert.ok([...fallback].length <= 50)
  assert.ok(fallback.length > 0)
})

test('menus preserve actual choices without padding or truncating them', () => {
  assert.deepEqual(storyChoices({nextDirections:[]}), [])
  assert.deepEqual(storyChoices({nextDirections:[{id:'stub',title:'继续当前目标'}]}), [])
  const directions = ['询问执事', '检查断牌', '拒绝结伴', '追问金屑'].map((title,i)=>({id:String(i),title,summary:title+'，等待明确结果。'}))
  assert.equal(storyChoices({nextDirections:directions.slice(0,1)}).length, 1)
  assert.equal(storyChoices({nextDirections:directions}).length, 4)
  assert.equal(storyChoices({nextDirections:[...directions,directions[0]]}).length, 4)
  const menu = [{id:'reader-one',title:'询问执事',summary:'我留在原地询问执事，等到明确答复。',evidence:['执事在场。']}]
  assert.deepEqual(storyChoices({nextDirections:directions,readerChoices:menu})[0].payload, {text:menu[0].summary})
  assert.equal(storyChoices({kind:'source_entry',nextDirections:[],openingActions:directions}).length,4)
})
