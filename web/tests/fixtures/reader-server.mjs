// Desktop UI fixture: serves the real build with in-memory API responses.
// Export with python3 -m test_support.export_reader_fixture /tmp/longform-reader.json
// Then run: node tests/fixtures/reader-server.mjs [port] /tmp/longform-reader.json
// Release a paused stream/menu with POST /__fixture/release or /__fixture/menu.
import http from 'node:http'
import { readFile } from 'node:fs/promises'
import { resolve, extname } from 'node:path'

const root = resolve(import.meta.dirname, '../../dist')
const sid = 'reader-fixture'
if (!process.argv[3]) throw new Error('请提供经过长篇清单校验的数据文件')
const novel = JSON.parse(await readFile(process.argv[3], 'utf8'))
if (novel.source_cjk < 100000 || !novel.source_sha256 || !novel.chapters.length) throw new Error('桌面夹具必须使用十万汉字长篇')
const state = novel.opening.branchState
const prose = sequence => novel.chapters[sequence % novel.chapters.length].text
const branch = (id, parentId, sequence) => ({ id, parentId, sequence, sessionId: sid,
  kind: parentId ? 'derivative' : 'source_entry',
  createdAt: new Date().toISOString(), narrativeText: prose(sequence),
  branchState: state, nextDirections: [], summary: `第${sequence + 1}页`, canonicalRelation: 'on_line' })
const nodes = [branch('root', null, 0)]
const receipts = new Map()
const displayed = new Set()
const failed = new Set()
const requests = []
let releaseStream = null
let menusOpen = false
let openingStarted = false
let failOpening = false
const menuWaiters = []
let journalMode = {}
let illustrationMode = {}
let journalScenario = false
const journalWaiters = []
const endedBranches = new Set()
const completedBranches = new Set()
const closureIntents = new Map()
const endingProposals = []
let closureMode = {}
const closureView = bid => ({ version: 'route-closure/fixture', branch_id: bid, root_branch_id: 'root',
  status: completedBranches.has(bid) ? 'completed' : endedBranches.has(bid) ? 'abandoned' : 'active',
  structure: { volume: null, arc: null, beat: novel.entry.beatId ?? null, source_progress: novel.opening.branchState?.sourceProgress ?? null },
  coverage: { goals: 'recorded', threads: 'recorded', state: 'recorded' },
  readiness: endedBranches.has(bid) ? 'ended' : closureMode.ready ? 'checklist_clear' : 'needs_explanation',
  outstanding: closureMode.ready ? [] : [{ id: 'goal', kind: 'goal', target_id: 'goal', title: novel.entry.openingThreads[0], reason: '桌面夹具：此事尚未交代。', required_disclosure: true, blockers: [], evidence: null }], cleared: [],
  outstanding_count: closureMode.ready ? 0 : 1, cleared_count: 0, ending_written: completedBranches.has(bid),
  note: '桌面夹具只验证页面交互，不代表正文质量验收。',
  lifecycle: { intended_type: closureIntents.get(bid) ?? null, ending_type: completedBranches.has(bid) ? closureIntents.get(bid) : endedBranches.has(bid) ? 'early' : null,
    intent_branch_id: closureIntents.has(bid) ? bid : null,
    phase: endedBranches.has(bid) ? 'ended' : closureIntents.has(bid) ? 'closing' : 'active',
    receipt: completedBranches.has(bid) ? { ending_type: closureIntents.get(bid) ?? 'normal', branch_id: bid, closed_at: new Date().toISOString(), readiness: 'checklist_clear', coverage: { goals: 'recorded', threads: 'recorded', state: 'recorded' }, outstanding: [], cleared: [], ending_written: true, proposal_id: 'proposal-0', binding_digest: 'fixture-binding' } : endedBranches.has(bid) ? { ending_type: 'early', branch_id: bid, closed_at: new Date().toISOString(), readiness: 'needs_explanation', coverage: { goals: 'recorded', threads: 'recorded', state: 'recorded' }, outstanding: [], cleared: [], ending_written: false } : null,
    requires_ending_evidence: true, ending_written: completedBranches.has(bid) } })
const json = (res, value, status = 200) => {
  res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value))
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  const path = url.pathname
  let body = ''
  for await (const chunk of req) body += chunk
  const input = body ? JSON.parse(body) : {}
  requests.push({ method: req.method, path, request_id: input.request_id, parent: input.parent_branch_id,
    subscriber: input.subscriber_id, draw: input.draw, history_id: input.history_id, choice_id: input.choice_id })
  if (path === '/__fixture/closure' && req.method === 'POST') {
    closureMode = input
    return json(res, { ok: true })
  }
  if (path.endsWith('/route-closure')) {
    const bid = input.branch_id ?? url.searchParams.get('branch_id')
    if (req.method === 'POST') {
      closureIntents.set(bid, input.intended_type)
      endingProposals.filter(p => p.branch_id === bid && ['pending', 'approved'].includes(p.status)).forEach(p => { p.status = 'stale' })
    }
    return json(res, closureView(bid))
  }
  if (path.endsWith('/ending-proposals')) {
    if (req.method === 'GET') return json(res, endingProposals.filter(p => p.branch_id === url.searchParams.get('branch_id')).slice(-10).reverse())
    const previous = endingProposals.find(p => p.branch_id === input.branch_id && p.request_id === input.request_id)
    if (previous) return json(res, previous)
    const node = nodes.find(n => n.id === input.branch_id)
    if (!closureMode.ready || !closureIntents.get(input.branch_id) || !node?.narrativeText.includes(input.ending_quote))
      return json(res, { error: { code: 'ending_not_ready', message: '收束条件尚未满足。' } }, 409)
    const status = closureMode.review ?? 'approved'
    const checks = Object.fromEntries(['ending_type_supported', 'threads_accounted_for', 'no_new_unresolved_conflict', 'ending_present'].map(key => [key, { passed: status === 'approved', evidence: input.ending_quote, reason: '桌面夹具：只验证交互，不代表正文已形成结局。' }]))
    const proposal = { ...input, id: `proposal-${endingProposals.length}`, binding_digest: 'fixture-binding', created_at: new Date().toISOString(), ending_type: closureIntents.get(input.branch_id), status,
      can_cancel: status === 'pending',
      review: ['approved', 'rejected'].includes(status) ? { decision: status === 'approved' ? 'allow' : 'reject', checks } : null,
      audit: { model: 'desktop-fixture', prompt_version: 'fixture', input: { ending_type: closureIntents.get(input.branch_id), outcome_summary: input.outcome_summary, ending_quote: input.ending_quote, narrative: node.narrativeText, goals: [], threads: [], conflicts: [], character_outcomes: [] }, raw_response: null, failure: null, metrics: null, observations: [] } }
    endingProposals.push(proposal)
    if (closureMode.lose_response) { closureMode.lose_response = false; res.destroy(); return }
    return json(res, proposal)
  }
  if (path.includes('/ending-proposals/') && path.endsWith('/commit')) {
    const proposal = endingProposals.find(p => p.id === path.split('/').at(-2) && p.branch_id === input.branch_id)
    if (!proposal || !['approved', 'committed'].includes(proposal.status)) return json(res, { error: { message: '提案尚未通过或已失效。' } }, 409)
    if (closureMode.commit_fail) return json(res, { error: { message: '模拟提交失败，路线未结束。' } }, 503)
    proposal.status = 'committed'
    endedBranches.add(input.branch_id); completedBranches.add(input.branch_id)
    return json(res, { status: 'completed', receipt: closureView(input.branch_id).lifecycle.receipt })
  }
  if (path.includes('/ending-proposals/') && path.endsWith('/cancel')) {
    const proposal = endingProposals.find(p => p.id === path.split('/').at(-2) && p.branch_id === input.branch_id)
    if (!proposal) return json(res, { error: { message: '没有这份提案。' } }, 404)
    if (!proposal.can_cancel && proposal.status !== 'cancelled')
      return json(res, { error: { message: '审查已经完成，请刷新查看结果。' } }, 409)
    proposal.status = 'cancelled'; proposal.can_cancel = false
    return json(res, proposal)
  }
  if (path === '/__fixture/illustrations' && req.method === 'POST') {
    illustrationMode = input
    return json(res, { ok: true })
  }
  if (path === '/__fixture/state') return json(res, { requests, saved: nodes.length, paused: !!releaseStream, displayed: [...displayed] })
  if (path.endsWith('/reading-receipts') && req.method === 'POST') {
    const node = nodes.find(n => n.id === input.branch_id && n.parentId)
    if (!node) return json(res, { recorded: false, reason: 'measurement_unavailable' })
    const duplicate = displayed.has(node.id)
    displayed.add(node.id)
    return json(res, { recorded: true, deduplicated: duplicate })
  }
  if (path === '/__fixture/journal' && req.method === 'POST') {
    journalMode = input
    if (input.seed_branches && !journalScenario) {
      journalScenario = true
      nodes.push(branch('left', 'root', 1), branch('right', 'root', 2))
    }
    if (input.release) journalWaiters.splice(0).forEach(resolve => resolve())
    return json(res, { ok: true, pending: journalWaiters.length })
  }
  if (path === '/__fixture/mode' && req.method === 'POST') {
    failOpening = input.fail_opening === true
    if (input.reset_menu) menusOpen = false
    return json(res, { ok: true })
  }
  if (path === '/__fixture/release' && req.method === 'POST') {
    releaseStream?.(); releaseStream = null; return json(res, { ok: true })
  }
  if (path === '/__fixture/menu' && req.method === 'POST') {
    menusOpen = true; menuWaiters.splice(0).forEach(resolve => resolve()); return json(res, { ok: true })
  }
  if (path === '/api/v1/health') return json(res, { phase: 'play', generation_available: true, state_updates_available: true })
  if (path === `/api/v1/sessions/${sid}`) return json(res, { session: { id: sid,
    storyPackageId: novel.package_id, storyPackageVersion: novel.version, title: `${novel.title} · 桌面功能验证` } })
  if (path.startsWith('/api/v1/packages/')) return json(res, {
    package: { title: `${novel.title} · 桌面功能验证`, summary: novel.entry.summary },
    characters: (novel.characters ?? [novel.character]).map(c => ({ id: c.id, name: c.name, defaultEntryPointId: c.defaultEntryPointId, identitySummary: c.identitySummary })),
    locations: [novel.location],
    entries: (novel.entries ?? [novel.entry]).map(e => ({ id: e.id, title: e.title, source_character_ids: e.sourceCharacterIds, beat_id: e.beatId, summary: e.summary })),
  })
  if (path === '/api/v1/sessions/stream') {
    openingStarted = true
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
    const event = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)
    if (!failed.has(input.request_id) && !receipts.has(input.request_id)) {
      event('delta', { text: nodes[0].narrativeText })
      await new Promise(resolve => { releaseStream = resolve; res.on('close', resolve) })
      if (res.destroyed) return
      if (failOpening) {
        failed.add(input.request_id)
        event('error', { code: 'generation_failed', message: '模拟开局失败', status: 503 }); return res.end()
      }
    }
    const result = { session: { id: sid }, branch: nodes[0], deduplicated: receipts.has(input.request_id) }
    receipts.set(input.request_id, result)
    event('done', result); return res.end()
  }
  if (path.endsWith('/branches/stream')) {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
    const event = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)
    if (endedBranches.has(input.parent_branch_id)) {
      event('error', { code: 'route_ended', message: '这条路线已收尾。', status: 409 }); return res.end()
    }
    if (receipts.has(input.request_id)) {
      event('done', { ...receipts.get(input.request_id), deduplicated: true }); return res.end()
    }
    const retry = failed.has(input.request_id)
    const next = branch(`page-${nodes.length}`, input.parent_branch_id, nodes.length)
    if (!retry) {
      event('delta', { text: input.text === '模拟中断' ? '这是一段尚未确认的草稿。' : next.narrativeText })
      await new Promise(resolve => { releaseStream = resolve; res.on('close', resolve) })
      if (res.destroyed) return
      if (input.text === '模拟中断') {
        failed.add(input.request_id)
        event('error', { code: 'generation_failed', message: '模拟断流', status: 503 }); return res.end()
      }
    }
    nodes.push(next)
    const result = { status: 'written', request_id: input.request_id, branch: next, deduplicated: false }
    receipts.set(input.request_id, result)
    event('done', result); return res.end()
  }
  if (path.endsWith('/end')) {
    endedBranches.add(input.branch_id); return json(res, { status: 'abandoned' })
  }
  if (path.endsWith('/choices/prepare')) {
    if (endedBranches.has(input.parent_branch_id)) return json(res, { error: { code: 'route_ended', message: '这条路线已收尾。' } }, 409)
    if ((openingStarted || input.parent_branch_id !== 'root') && !menusOpen) await new Promise(resolve => menuWaiters.push(resolve))
    return json(res, { parent_branch_id: input.parent_branch_id, choices: [1, 2].map(i => ({
      id: `${input.parent_branch_id}-${i}`, draft_id: `draft-${input.parent_branch_id}-${i}`,
      title: i === 1 ? '询问山门情况' : '留在原处等待', summary: '只处理眼前的行动。', status: 'ready',
    })) })
  }
  if (path.endsWith('/release') || path.endsWith('/shown')) return json(res, { ok: true })
  if (path.endsWith('/illustrations')) return json(res, {
    available: false, can_generate: false, items: [], ...illustrationMode,
  })
  if (path.endsWith('/journey')) {
    const bid = url.searchParams.get('branch_id')
    const mode = journalMode
    if (mode.pause_branch === bid) await new Promise(resolve => journalWaiters.push(resolve))
    if (mode.fail) return json(res, { error: { message: '模拟手记失败' } }, 503)
    const changed = bid === 'left'
    const journal = { branch_id: mode.mismatch ? 'another-branch' : bid,
    role_name: novel.character.name, goal: novel.entry.openingThreads[0], progress: null, progress_label: '沿途经历', choices_made: nodes.length - 1,
    current_task: '', status: 'active', location: novel.location.name, feedback: [], clues: [], people: [], relationships: [],
    lineage: nodes.map(n => n.id), milestones: [], recap: [],
    }
    if (journalScenario) Object.assign(journal, {
      goal: changed ? '寻找新的落脚处' : '等待同行者回答',
      choices_made: bid === 'root' ? 0 : 1,
      goals: changed ? [{ id: 'old', title: '等待同行者回答', status: 'abandoned', reason: '你已放下等待。' },
        { id: 'new', title: '寻找新的落脚处', status: 'active' }] : [{ id: 'old', title: '等待同行者回答', status: 'active' }],
      clues: changed ? ['断牌上的刻痕已经核实。'] : [],
      threads: changed ? [
        { id: 'visitor', title: '敲门的人是谁', status: 'resolved', reason: '你已核实来者身份。', evidence: '你确认刚才是自己敲的门。', causeBranchId: 'left' },
        { id: 'marks', title: '断牌的刻痕来自哪里', status: 'abandoned', reason: '你明确决定不再追查。', evidence: '你把断牌收好，决定不再追查刻痕的来历。', causeBranchId: 'left' },
        { id: 'shelter', title: '石屋是否可以避雨', status: 'open', reason: '你尚未进入石屋。', evidence: '石屋的门还关着，能否避雨仍不清楚。', causeBranchId: 'left' },
      ] : [{ id: 'visitor', title: '敲门的人是谁', status: bid === 'right' ? 'unknown' : 'open', reason: '', evidence: '', causeBranchId: null }],
      lineage: bid === 'root' ? ['root'] : ['root', bid],
      people: [{ id: 'companion', name: '同行者', is_player: false, first_page: 1, last_page: 1,
        identity: '身份随剧情逐渐明确', summary: '你在山门遇见的人。', summarized: true,
        portrait: { url: null, source: 'preset', fallback_key: 'person-1' }, name_evidence: null,
        status: { code: changed ? 'dead' : 'unknown', label: changed ? '已死亡' : '状态未确认',
          permanence: changed ? 'permanent' : null, evidence: changed ? {
            branch_id: 'left', page: 2, quote: '同行者已经死亡。', source: 'consequence' } : null } }],
    })
    if (endedBranches.has(bid)) journal.status = completedBranches.has(bid) ? 'completed' : 'abandoned'
    if (mode.character_status && journal.people.length) {
      const labels = { injured: '受伤', missing: '失踪' }
      if (labels[mode.character_status]) journal.people[0].status = {
        code: mode.character_status, label: labels[mode.character_status], permanence: 'temporary',
        evidence: { branch_id: bid, page: 3, quote: '桌面夹具注入的状态证据，仅验证界面。', source: 'consequence' },
      }
    }
    return json(res, journal)
  }
  if (path.endsWith('/state')) return json(res, { state })
  if (path === '/api/v1/context/build') return json(res, { context: {
    currentChapter: { title: novel.entry.chapterTitle }, locations: [novel.location],
  } })
  if (path.endsWith('/source-chapter')) return json(res, { title: novel.chapters[0].title, text: novel.chapters[0].text })
  if (path.endsWith('/branches')) return json(res, { branches: nodes.map(n => ({
    id: n.id, parent_id: n.parentId, sequence: n.sequence, created_at: n.createdAt, action: n.summary,
  })), next_after_sequence: null })
  if (path.includes('/branches/')) return json(res, nodes.find(n => n.id === path.split('/').at(-1)), 200)
  if (path.startsWith('/api/')) return json(res, { error: { message: path } }, 404)
  const file = resolve(root, `.${path === '/' || !extname(path) ? '/index.html' : path}`)
  if (!file.startsWith(root + '/')) return json(res, {}, 403)
  try {
    const content = await readFile(file)
    res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' })[extname(file)] ?? 'application/octet-stream' })
    res.end(content)
  } catch { res.writeHead(404); res.end() }
})
server.listen(Number(process.argv[2] ?? 8766), '127.0.0.1', () => console.log(`Reader fixture: http://127.0.0.1:${server.address().port}/sessions/${sid}`))
