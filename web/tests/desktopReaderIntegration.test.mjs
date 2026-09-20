import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { spawn, spawnSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const ROOT = new URL('../..', import.meta.url).pathname
const PACKAGE_ID = 'taixu-relics-part1'
const VERSION = '0.1.3'

async function startFixture() {
  const directory = await mkdtemp(join(tmpdir(), 'ose-desktop-'))
  const fixture = join(directory, 'reader.json')
  const exported = spawnSync('/Users/James/open-story-engine/.venv-api/bin/python', [
    '-B', '-m', 'test_support.export_reader_fixture', fixture, '--package-id', PACKAGE_ID,
  ], { cwd: ROOT, encoding: 'utf8' })
  assert.equal(exported.status, 0, exported.stderr)
  const novel = JSON.parse(await readFile(fixture, 'utf8'))
  assert.equal(novel.version, VERSION)
  assert.ok(novel.source_cjk >= 100000)
  assert.equal(novel.characters.length, 7)
  assert.equal(novel.entries.length, 7)
  const process = spawn('node', ['web/tests/fixtures/reader-server.mjs', '0', fixture], {
    cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
  })
  const output = await new Promise((resolve, reject) => {
    let text = ''
    const timer = setTimeout(() => reject(new Error('reader-server 启动超时')), 5000)
    process.stdout.on('data', chunk => {
      text += chunk.toString()
      const match = text.match(/Reader fixture: (http:\/\/127\.0\.0\.1:\d+)/)
      if (match) { clearTimeout(timer); resolve(match[1]) }
    })
    process.once('exit', code => reject(new Error(`reader-server 退出：${code}\n${text}`)))
  })
  return { base: output, novel, process, directory }
}

async function stopFixture(fixture) {
  fixture.process.kill('SIGTERM')
  await new Promise(resolve => fixture.process.once('exit', resolve))
  await rm(fixture.directory, { recursive: true, force: true })
}

async function json(base, path, init = {}) {
  const response = await fetch(`${base}${path}`, { headers: { 'Content-Type': 'application/json' }, ...init })
  const body = await response.json()
  return { response, body }
}

async function sse(base, path, payload) {
  const response = await fetch(`${base}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
  })
  assert.equal(response.status, 200)
  const release = await fetch(`${base}/__fixture/release`, { method: 'POST' })
  assert.equal(release.status, 200)
  const text = await response.text()
  const events = [...text.matchAll(/event: ([^\n]+)\ndata: ([^\n]+)/g)].map(match => ({ event: match[1], data: JSON.parse(match[2]) }))
  return events
}

test('official longform desktop reader covers identity entry, turns, recovery, closure and branch restore', async t => {
  const fixture = await startFixture()
  t.after(() => stopFixture(fixture))
  const { base, novel } = fixture

  const page = await fetch(`${base}/`)
  assert.equal(page.status, 200)
  assert.match(await page.text(), /id="root"/)

  const catalog = await json(base, `/api/v1/packages/${PACKAGE_ID}/${VERSION}`)
  assert.equal(catalog.response.status, 200)
  assert.equal(catalog.body.characters.length, 7)
  for (const character of catalog.body.characters) {
    const entry = catalog.body.entries.find(item => item.id === character.defaultEntryPointId)
    assert.ok(entry, `${character.id} 缺少默认入口`)
    assert.deepEqual(entry.source_character_ids, [character.id])
    const events = await sse(base, '/api/v1/sessions/stream', {
      package: { package_id: PACKAGE_ID, version: VERSION }, entry_point_id: entry.id,
      source_character_id: character.id, identity_opening: true, request_id: `opening-${character.id}`,
    })
    assert.equal(events.at(-1).event, 'done')
    assert.equal(events.at(-1).data.branch.id, 'root')
  }

  await json(base, '/__fixture/menu', { method: 'POST', body: '{}' })
  const choices = await json(base, '/api/v1/sessions/reader-fixture/choices/prepare', {
    method: 'POST', body: JSON.stringify({ parent_branch_id: 'root', subscriber_id: 'desktop-test' }),
  })
  assert.equal(choices.body.choices.length, 2)
  const preset = await sse(base, '/api/v1/sessions/reader-fixture/branches/stream', {
    parent_branch_id: 'root', choice_id: choices.body.choices[0].id, draft_id: choices.body.choices[0].draft_id,
    request_id: 'turn-preset',
  })
  assert.equal(preset.at(-1).event, 'done')
  const freeText = await sse(base, '/api/v1/sessions/reader-fixture/branches/stream', {
    parent_branch_id: 'page-1', text: '自由行动：我沿着山门石阶继续观察。', request_id: 'turn-free-text',
  })
  assert.equal(freeText.at(-1).event, 'done')

  const failed = await sse(base, '/api/v1/sessions/reader-fixture/branches/stream', {
    parent_branch_id: 'page-2', text: '模拟中断', request_id: 'turn-retry',
  })
  assert.equal(failed.at(-1).event, 'error')
  assert.equal(failed.at(-1).data.code, 'generation_failed')
  const recovered = await sse(base, '/api/v1/sessions/reader-fixture/branches/stream', {
    parent_branch_id: 'page-2', text: '模拟中断', request_id: 'turn-retry',
  })
  assert.equal(recovered.at(-1).event, 'done')
  assert.equal(recovered.at(-1).data.deduplicated, false)

  await json(base, '/__fixture/journal', { method: 'POST', body: JSON.stringify({ seed_branches: true, character_status: 'dead' }) })
  const journal = await json(base, '/api/v1/sessions/reader-fixture/journey?branch_id=left')
  assert.equal(journal.body.people[0].status.code, 'dead')
  assert.equal(journal.body.people[0].status.permanence, 'permanent')

  await json(base, '/__fixture/closure', { method: 'POST', body: JSON.stringify({ ready: true }) })
  const branchText = novel.chapters[1].text
  const plan = await json(base, '/api/v1/sessions/reader-fixture/route-closure', {
    method: 'POST', body: JSON.stringify({ branch_id: 'page-1', intended_type: 'normal' }),
  })
  assert.equal(plan.body.readiness, 'checklist_clear')
  const proposal = await json(base, '/api/v1/sessions/reader-fixture/ending-proposals', {
    method: 'POST', body: JSON.stringify({ branch_id: 'page-1', request_id: 'ending-1', outcome_summary: '这一段旅程暂告一段落。', ending_quote: branchText.slice(0, 20) }),
  })
  assert.equal(proposal.body.review.decision, 'allow')
  assert.equal(Object.keys(proposal.body.review.checks).length, 4)
  const committed = await json(base, '/api/v1/sessions/reader-fixture/ending-proposals/proposal-0/commit', {
    method: 'POST', body: JSON.stringify({ branch_id: 'page-1' }),
  })
  assert.equal(committed.body.status, 'completed')
  assert.equal(committed.body.receipt.ending_written, true)
  const ended = await json(base, '/api/v1/sessions/reader-fixture/journey?branch_id=page-1')
  assert.equal(ended.body.status, 'completed')
  const continuation = await sse(base, '/api/v1/sessions/reader-fixture/branches/stream', {
    parent_branch_id: 'page-1', text: '结局后普通续写不应继续', request_id: 'after-ending',
  })
  assert.equal(continuation.at(-1).event, 'error')
  assert.equal(continuation.at(-1).data.code, 'route_ended')

  const branches = await json(base, '/api/v1/sessions/reader-fixture/branches')
  assert.ok(branches.body.branches.some(item => item.id === 'page-1'))
  const restored = await json(base, '/api/v1/sessions/reader-fixture/branches/page-1')
  assert.equal(restored.body.parentId, 'root')
  const closure = await json(base, '/api/v1/sessions/reader-fixture/route-closure?branch_id=page-1')
  assert.equal(closure.body.lifecycle.ending_written, true)
})
