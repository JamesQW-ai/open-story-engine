import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSync } from 'esbuild'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { illustrationAnchor, readingSections, intervalIllustrationDue } from '../src/components/readingLayout.ts'

test('short-page cadence follows the chosen ancestry and excludes sibling branches', () => {
  const branches = [{id:'root', parent_id:null}, {id:'other', parent_id:'root'},
    {id:'first', parent_id:'root'}, {id:'second', parent_id:'first'}, {id:'third', parent_id:'second'}]
  assert.equal(intervalIllustrationDue('second', branches), false)
  assert.equal(intervalIllustrationDue('third', branches), true)
  assert.equal(intervalIllustrationDue('missing', branches), false)
  assert.equal(intervalIllustrationDue('a', [{id:'a',parent_id:'b'}, {id:'b',parent_id:'a'}]), false)
})

test('inline slot appears after its complete paragraph and stays there through replay completion', () => {
  const directory = mkdtempSync(join(tmpdir(), 'story-prose-test-'))
  try {
    const compiled = join(directory, 'component.cjs')
    buildSync({ stdin: { contents: `
      import { createElement } from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { StoryProse } from './src/components/StoryProse';
      export const render = props => renderToStaticMarkup(createElement(StoryProse, props));
    `, resolveDir: fileURLToPath(new URL('../', import.meta.url)), loader: 'tsx' },
    bundle: true, platform: 'node', format: 'cjs', outfile: compiled, logLevel: 'silent', jsx: 'automatic' })
    const { render } = createRequire(import.meta.url)(compiled)
    const novel = JSON.parse(readFileSync(new URL('../../content/packages/taixu-relics-part1/0.1.3/reader.json', import.meta.url), 'utf8'))
    const fullText = novel.chapters[0].text
    const paragraphs = readingSections(fullText).flatMap(section => section.paragraphs)
    const anchor = illustrationAnchor(fullText)
    const prefix = paragraphs.slice(0, anchor + 1).join('\n')
    const props = { fullText, title: '', opening: false, streaming: true, sessionId: 'temporary', branchId: 'saved' }
    const before = render({ ...props, text: prefix.slice(0, -1) })
    assert.ok(!before.includes('inline-scene-slot'))
    const during = render({ ...props, text: prefix })
    const after = render({ ...props, text: fullText, streaming: false })
    for (const html of [during, after]) {
      const [leading, trailing] = html.split('class="inline-scene-slot"')
      assert.equal((leading.match(/class="passage"/g) ?? []).length, anchor + 1)
      assert.ok(trailing.includes('正文继续展开'))
      assert.equal(html.split('inline-scene-slot').length, 2)
    }
    assert.ok(after.indexOf('inline-scene-slot') < after.lastIndexOf('class="passage"'))
    assert.ok(after.includes('水墨过渡图，不描绘具体剧情'))
    const shortText = fullText.slice(0, 300)
    assert.ok(!render({ ...props, text: shortText, fullText: shortText, streaming: false }).includes('水墨过渡图'))
    const fourth = render({ ...props, text: shortText, fullText: shortText, streaming: false, intervalIllustration: true })
    assert.ok(fourth.includes('水墨过渡图'))
    assert.equal(fourth.split('inline-scene-slot').length, 2)
    const opening = render({ ...props, text: fullText, streaming: false,
      fixedOpeningImage: { url: '/opening.png', alt: '预设图', source: 'published' } })
    assert.ok(opening.indexOf('inline-scene-slot') < opening.indexOf('/opening.png'))
    assert.ok(opening.indexOf('/opening.png') < opening.lastIndexOf('class="passage"'))
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})
