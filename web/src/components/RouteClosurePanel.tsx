import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { EndingProposal, RouteClosure } from '../api/types'
import { closureActions, endingRequest } from './endingRequest'
import { messageOf } from './StoryUI'

const modes = { normal: '达成目标后收尾', deviation: '沿新的方向收尾', failure: '以失败结果收尾' }
const statuses = { pending: '正在核对结局', approved: '结局已核对，尚未结束', rejected: '当前正文还不能作为结局',
  failed: '结局核对未完成', stale: '故事或结局方向已变化，请重新核对', committed: '结局已保存', cancelled: '已取消本次核对' }
const checks: Record<string, string> = { ending_type_supported: '结局类型', threads_accounted_for: '问题交代',
  no_new_unresolved_conflict: '未解冲突', ending_present: '正文收尾' }

export function RouteClosurePanel({ sessionId, branchId, narrative, canEdit, onChanged }: {
  sessionId: string; branchId: string; narrative: string; canEdit: boolean; onChanged: (ended?: boolean) => void
}) {
  const [closure, setClosure] = useState<RouteClosure | null>(null)
  const [proposals, setProposals] = useState<EndingProposal[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [summary, setSummary] = useState('')
  const [quote, setQuote] = useState('')
  const [confirm, setConfirm] = useState<string | null>(null)
  const [earlyConfirm, setEarlyConfirm] = useState(false)
  const [fresh, setFresh] = useState(false)
  const actions = useRef<ReturnType<typeof closureActions> | null>(null)
  const request = useRef<ReturnType<typeof endingRequest> | null>(null)

  async function read() {
    const [next, history] = await Promise.all([api.routeClosure(sessionId, branchId), api.endingProposals(sessionId, branchId)])
    if (next.branch_id !== branchId || history.some(p => p.branch_id !== branchId)) throw new Error('收束记录与当前页不一致，请刷新。')
    return { next, history }
  }
  function show({ next, history }: Awaited<ReturnType<typeof read>>) {
    setClosure(next); setProposals(history); setConfirm(null)
  }
  function refresh() {
    setError('')
    void actions.current?.run(read, show)
  }
  useEffect(() => {
    const controller = closureActions({ busy: setBusy, error: e => setError(messageOf(e)) })
    actions.current = controller
    try {
      request.current = endingRequest(`story-ending:${sessionId}:${branchId}`, sessionStorage)
      const saved = request.current.saved()
      if (saved) { setSummary(saved.outcome_summary); setQuote(saved.ending_quote) }
    } catch { setError('无法保存审查请求，请允许本页使用会话存储后重试。') }
    void controller.run(read, show)
    return () => controller.dispose()
  }, [sessionId, branchId])

  const proposal = fresh ? undefined : proposals[0]
  const ended = closure?.status !== 'active'
  const editable = canEdit && !ended && !busy
  const ready = closure?.readiness === 'checklist_clear'
  const canPropose = editable && ready && !!summary.trim() && !!quote.trim() && narrative.includes(quote) && !proposal
  function propose() {
    if (!canPropose) return
    setError('')
    void actions.current?.run(async () => {
      if (!request.current) throw new Error('无法保存审查请求，请刷新后重试。')
      // Ending type is an internal review result. The player supplies prose
      // evidence; the service starts a neutral closeout plan when needed.
      if (!closure?.lifecycle?.intended_type) {
        const planned = await api.planClosure(sessionId, branchId, 'normal')
        if (planned.branch_id !== branchId) throw new Error('收束记录与当前页不一致，请刷新。')
        setClosure(planned)
      }
      const attempt = request.current.prepare(summary, quote)
      const result = await api.proposeEnding(sessionId, branchId, attempt)
      if (result.branch_id !== branchId) throw new Error('审查结果与当前页不一致，请刷新。')
      return result
    }, result => { setProposals(items => [result, ...items.filter(p => p.id !== result.id)]); setFresh(false) })
  }
  function commit(id: string) {
    setError('')
    void actions.current?.run(() => api.commitEnding(sessionId, branchId, id), () => {
      setConfirm(null)
      setProposals(items => items.map(p => p.id === id ? { ...p, status: 'committed' } : p))
      setClosure(value => value ? { ...value, status: 'completed' } : value)
      onChanged(true)
    })
  }
  function cancel(id: string) {
    setError('')
    void actions.current?.run(() => api.cancelEnding(sessionId, branchId, id), result => {
      if (result.branch_id !== branchId || result.id !== id) throw new Error('审查结果与当前页不一致，请刷新。')
      setProposals(items => items.map(p => p.id === id ? result : p)); setConfirm(null)
    })
  }
  function endEarly() {
    setError('')
    void actions.current?.run(() => api.endRoute(sessionId, branchId), () => {
      setEarlyConfirm(false)
      setClosure(value => value ? { ...value, status: 'abandoned' } : value)
      onChanged(true)
    })
  }
  return <section className="closure-panel" aria-label="路线收束">
    <h3>为这段经历收尾</h3>
    <button className="text-button" disabled={busy} onClick={refresh}>刷新</button>
    {busy && <p role="status">正在处理，请稍候。审查不会自动结束路线。</p>}
    {error && <p role="alert">{error}</p>}
    {closure && <>
      {ended && <p>{closure.lifecycle?.ending_type === 'early' ? '你已提前结束这条路线。' : '这条路线已结束。'}已读正文保留，可回到更早的选择开启新分支。</p>}
      <h4>{ended ? '结束时未交代的事' : '还需要交代的事'}</h4>
      {(closure.lifecycle?.receipt?.outstanding ?? closure.outstanding).length > 0 ?
        <ul>{(closure.lifecycle?.receipt?.outstanding ?? closure.outstanding).map(item =>
          <li key={item.id}><strong>{item.title}</strong><p>{item.reason}</p></li>)}</ul> :
        <p>{closure.readiness === 'blocked_unknown' ? '现有记录不足，是否已交代完毕仍未确认。'
          : ended ? '当前记录没有列出未交代事项。' : '当前清单没有待交代事项；这不等于正文已经形成结局。'}</p>}
      {!ended && ready && !proposal && <div className="ending-evidence">
        <p>如果当前正文已经收尾，可以提交结局说明。通过核对后仍需你确认保存。</p>
        <label>这段经历如何结束
          <textarea aria-label="结局说明" maxLength={2000} value={summary} disabled={!editable} onChange={e => setSummary(e.target.value)} />
        </label>
        <label>从当前正文复制收尾原句
          <textarea aria-label="收尾原句" maxLength={6000} value={quote} disabled={!editable} onChange={e => setQuote(e.target.value)} />
        </label>
        {!!quote && !narrative.includes(quote) && <p>原句需与当前页正文完全一致。</p>}
        <button className="button subtle" disabled={!canPropose} onClick={propose}>提交结局审查</button>
      </div>}
      {!ended && <div className="early-close">
        <p>如果你想停在当前页，可以把这条路线保存为提前结束，已读正文仍会保留。</p>
        {earlyConfirm ? <div className="closure-confirm" role="group" aria-label="确认提前结束">
          <p>确认在当前页结束这条路线？</p>
          <button className="button primary" disabled={!editable} onClick={endEarly}>确认提前结束</button>
          <button className="text-button" disabled={busy} onClick={() => setEarlyConfirm(false)}>继续探索</button>
        </div> : <button className="text-button" disabled={!editable} onClick={() => setEarlyConfirm(true)}>在这里结束这条路线</button>}
      </div>}
    </>}
    {proposal && <div className="ending-review" aria-live="polite">
      <h4>{statuses[proposal.status]}</h4>
      <p>{modes[proposal.ending_type]} · {proposal.outcome_summary}</p>
      <blockquote>{proposal.ending_quote}</blockquote>
      {proposal.review && <ul>{Object.entries(proposal.review.checks).map(([id, check]) =>
        <li key={id}><strong>{checks[id] ?? '审查项'} · {check.passed ? '通过' : '未通过'}</strong><p>{check.reason}</p>
          {check.evidence && <blockquote>{check.evidence}</blockquote>}</li>)}</ul>}
      {proposal.can_cancel && <>
        <p>可以刷新查看结果；若审查长时间未完成，可放弃本次审查后重新提交。放弃后迟到结果不会生效，已发送请求仍可能产生用量。</p>
        <button className="text-button" disabled={!editable} onClick={() => cancel(proposal.id)}>放弃本次审查</button>
      </>}
      {proposal.status === 'cancelled' && <p>这条路线仍可继续，之后可以再次提交结局说明。</p>}
      {proposal.status === 'failed' && <p>这次核对没有完成，路线仍可继续。之后可以再次提交。</p>}
      {proposal.status === 'approved' && !ended && <>
        <p>保存后，本路线将停止续写；回到更早的选择会产生新的分支历史。</p>
        {confirm === proposal.id ? <div className="closure-confirm" role="group" aria-label="确认保存结局">
          <p>确认以这份审查通过的正文作为结局？</p>
          <button className="button primary" disabled={!editable} onClick={() => commit(proposal.id)}>确认保存结局</button>
          <button className="text-button" disabled={busy} onClick={() => setConfirm(null)}>暂不结束</button>
        </div> : <button className="button primary" disabled={!editable} onClick={() => setConfirm(proposal.id)}>保存为本路线结局</button>}
      </>}
      {['failed', 'rejected', 'stale', 'cancelled'].includes(proposal.status) && !proposal.can_cancel && editable && <button className="text-button" onClick={() => {
        try { request.current?.clear(); setSummary(proposal.outcome_summary); setQuote(proposal.ending_quote); setFresh(true); setError('') }
        catch { setError('无法保存新的审查请求，请检查本页存储设置。') }
      }}>重新填写证据</button>}
    </div>}
  </section>
}
