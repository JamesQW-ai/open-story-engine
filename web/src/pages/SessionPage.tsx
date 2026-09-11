import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { storyChoices } from '../components/storyPresentation'
import { StoryProse } from '../components/StoryProse'
import { CharacterGraph } from '../components/CharacterGraph'
import { api } from '../api/client'
import type {
  Journey,
  JournalPerson,
  BranchSummaryItem,
  BranchView,
  ContextView,
  PackageCatalog,
  PlayContinueRequest,
  SessionView,
  SourceChapterView,
  StateView,
} from '../api/types'
import { Loading, messageOf, Notice, storyImage } from '../components/StoryUI'

type Attempt = PlayContinueRequest & { request_id: string }
export function SessionPage() {
  const { sessionId = '' } = useParams()
  const location = useLocation()
  const [session, setSession] = useState<SessionView | null>(null)
  const [catalog, setCatalog] = useState<PackageCatalog | null>(null)
  const [branches, setBranches] = useState<BranchSummaryItem[]>([])
  const [selected, setSelected] = useState<BranchView | null>(null)
  const [chapter, setChapter] = useState<SourceChapterView | null>(null)
  const [state, setState] = useState<StateView | null>(null)
  const [journal, setJournal] = useState<Journey | null>(null)
  const [rechoosing, setRechoosing] = useState(false)
  const [ending, setEnding] = useState(false)
  const [confirmEnd, setConfirmEnd] = useState(false)
  const endDialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    if (confirmEnd) endDialog.current?.showModal()
    else endDialog.current?.close()
  }, [confirmEnd])
  const [context, setContext] = useState<ContextView | null>(null)
  const [contextError, setContextError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [writing, setWriting] = useState(false)
  const [streamText, setStreamText] = useState('')
  const streamController = useRef<AbortController | null>(null)
  const [playable, setPlayable] = useState(false)
  const [freeText, setFreeText] = useState('')
  const [tab, setTab] = useState<'goal' | 'people' | 'clues'>(
    'goal',
  )
  const [railOpen, setRailOpen] = useState(false)
  const [personCard, setPersonCard] = useState<JournalPerson | null>(null)
  const personDialog = useRef<HTMLDialogElement>(null)
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileError, setProfileError] = useState('')
  const profileRequest = useRef(0)
  async function openPerson(card: JournalPerson) {
    setPersonCard(card)
    setProfileError('')
    const request = ++profileRequest.current
    if (card.summarized || !selected || !playable) return
    const bid = selected.id
    setProfileLoading(true)
    try {
      const updated = await api.characterProfile(sessionId, bid, card.id)
      if (request !== profileRequest.current) return
      setPersonCard(updated)
      setJournal((j) => j ? { ...j, people: j.people.map((p) => p.id === card.id ? updated : p) } : j)
    } catch {
      if (request === profileRequest.current) setProfileError('概况暂未更新')
    } finally {
      if (request === profileRequest.current) setProfileLoading(false)
    }
  }
  function closePerson() {
    profileRequest.current++
    setProfileLoading(false)
    setPersonCard(null)
  }
  useEffect(() => {
    if (personCard) personDialog.current?.showModal()
    else personDialog.current?.close()
  }, [personCard])
  const [elapsed, setElapsed] = useState(0)
  const [retryable, setRetryable] = useState(false)
  const attempt = useRef<Attempt | null>(null)
  const selecting = useRef(0)
  const locked = useRef(false)
  const article = useRef<HTMLElement>(null)
  const notes = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    if (railOpen) notes.current?.showModal()
    else notes.current?.close()
  }, [railOpen])
  async function allBranches() {
    let after = -1
    const all: BranchSummaryItem[] = []
    for (;;) {
      const page = await api.getBranches(sessionId, after)
      all.push(...page.branches)
      if (page.next_after_sequence === null) break
      if (page.next_after_sequence <= after)
        throw new Error('阅读记录加载异常，请重试。')
      after = page.next_after_sequence
    }
    return all
  }
  async function selectBranch(id: string) {
    const seq = ++selecting.current
    setLoading(true)
    setJournal(null)
    closePerson()
    setRechoosing(false)
    setError('')
    setContext(null)
    setContextError('')
    setState(null)
    setChapter(null)
    setRetryable(false)
    attempt.current = null
    try {
      const branch = await api.getBranch(sessionId, id)
      if (seq !== selecting.current) return
      setSelected(branch)
      setLoading(false)
      if (branch.canonicalRelation === 'on_line') {
        api
          .getSourceChapter(sessionId, id)
          .then((c) => {
            if (seq === selecting.current) setChapter(c)
          })
          .catch(() => undefined)
      }
      try {
        localStorage.setItem(`story-progress:${sessionId}`, id)
      } catch {
        /* 阅读不依赖本地存储 */
      }
      api
        .journey(sessionId, id)
        .then((j) => {
          if (seq === selecting.current) setJournal(j)
        })
        .catch(() => undefined)
      api
        .getState(sessionId, id)
        .then((s) => {
          if (seq === selecting.current) setState(s)
        })
        .catch(() => undefined)
      api
        .buildContext({ session_id: sessionId, parent_branch_id: id })
        .then((c) => {
          if (seq === selecting.current) setContext(c)
        })
        .catch(() => {
          if (seq === selecting.current)
            setContextError('这一页的补充信息暂不可用，仍可继续阅读。')
        })
      requestAnimationFrame(() => {
        article.current?.scrollTo(0, 0)
        window.scrollTo(0, 0)
      })
    } catch (e) {
      if (seq === selecting.current) {
        setError(messageOf(e))
        setLoading(false)
      }
    }
  }
  async function load() {
    setError('')
    setLoading(true)
    try {
      const [view, all, health] = await Promise.all([
        api.getSession(sessionId),
        allBranches(),
        api.health(),
      ])
      setSession(view)
      setBranches(all)
      setPlayable(health.phase === 'play' && health.generation_available)
      api
        .getPackage(
          view.session.storyPackageId,
          view.session.storyPackageVersion,
        )
        .then(setCatalog)
        .catch(() => undefined)
      let saved: string | null = null
      try {
        saved = localStorage.getItem(`story-progress:${sessionId}`)
      } catch {
        /* 退回最新记录 */
      }
      const focus = (location.state as { focus?: string } | null)?.focus
      const initial =
        all.find((b) => b.id === focus) ??
        all.find((b) => b.id === saved) ??
        all[all.length - 1]
      if (initial) await selectBranch(initial.id)
      else setLoading(false)
    } catch (e) {
      setError(messageOf(e))
      setLoading(false)
    }
  }
  useEffect(() => {
    void load()
    return () => {
      selecting.current++
      profileRequest.current++
      streamController.current?.abort()
    }
  }, [sessionId])
  useEffect(() => {
    if (!writing) return
    setElapsed(0)
    const timer = window.setInterval(() => setElapsed((t) => t + 1), 1000)
    return () => window.clearInterval(timer)
  }, [writing])
  async function runTurn(payload: Attempt) {
    if (locked.current) return
    locked.current = true
    setWriting(true)
    setStreamText('')
    streamController.current = new AbortController()
    requestAnimationFrame(() =>
      article.current?.scrollIntoView({ block: 'start' }),
    )
    setNotice('')
    setRetryable(false)
    try {
      const result = await api.streamTurn(
        sessionId,
        payload,
        (chunk) => setStreamText((text) => text + chunk),
        () => setStreamText(''),
        streamController.current.signal,
      )
      if (result.status === 'rejected' || !result.branch) {
        setNotice('这次行动未能继续故事，请换一个方向或调整描述。')
        attempt.current = null
        return
      }
      const branch = result.branch
      setFreeText('')
      // 正文一旦成功即可阅读，记录列表刷新失败不应遮蔽已经生成的结果。
      setBranches((prev) =>
        prev.some((b) => b.id === branch.id)
          ? prev
          : [
              ...prev,
              {
                id: branch.id,
                session_id: sessionId,
                parent_id: branch.parentId ?? null,
                sequence: branch.sequence,
                created_at: branch.createdAt,
                action: payload.text || selected?.nextDirections.find((d) => d.id === payload.direction_id)?.summary || selected?.nextDirections.find((d) => d.id === payload.direction_id)?.title || branch.summary,
              },
            ],
      )
      await selectBranch(branch.id)
      setStreamText('')
      allBranches()
        .then(setBranches)
        .catch(() =>
          setNotice('正文已保存，阅读记录刷新失败。重新打开故事即可恢复。'),
        )
      if (result.deduplicated) setNotice('已恢复上次生成的内容。')
    } catch (e) {
      setNotice('这次续写未能完成，请重试。')
      setRetryable(true)
    } finally {
      locked.current = false
      setWriting(false)
    }
  }
  function submit(payload: { direction_id?: string; text?: string }) {
    if (!selected || locked.current || loading) return
    const next = {
      parent_branch_id: selected.id,
      request_id: crypto.randomUUID(),
      ...payload,
    }
    attempt.current = next
    void runTurn(next)
  }
  if (!session)
    return (
      <main className="page">
        {error ? <Notice text={error} retry={load} /> : <Loading />}
      </main>
    )
  const storyTitle = catalog?.package.title ?? '我的故事'
  const currentState = state?.state ?? selected?.branchState
  const roleId = currentState?.playerCharacterId
  const role =
    catalog?.characters.find((c) => c.id === roleId)?.name ??
    (typeof currentState?.playerName === 'string'
      ? currentState.playerName
      : '你')
  const placeId =
    currentState?.playerLocationId ?? currentState?.currentLocationId
  const place = (context?.context.locations ?? catalog?.locations)?.find(
    (l) => l.id === placeId,
  )?.name
  const parent = selected?.parentId
    ? branches.find((b) => b.id === selected.parentId)
    : null
  const children = branches.filter((b) => b.parent_id === selected?.id)
  const people = journal?.people ?? []
  const routeEnded = journal && journal.status !== 'active'
  const reviewing = children.length > 0 && !rechoosing
  const readingText = writing
    ? streamText
    : (chapter?.text ?? selected?.narrativeText ?? '')
  const choices = selected ? storyChoices(selected, place) : []
  return (
    <main className="reader-layout immersive-reader">
      <section className="reader-main">
        <div className="reader-heading">
          <div>
            <Link to="/sessions" className="back-link">
              我的故事
            </Link>
            <h1>
              {storyTitle}
              <span>以{role}的身份</span>
            </h1>
          </div>
          <button
            className="button subtle rail-toggle"
            aria-expanded={railOpen}
            onClick={() => setRailOpen(!railOpen)}
          >
            故事手记 ☷
          </button>
        </div>
        <div className="reader-meta">
          <span>
            {chapter?.title ??
              (selected?.entryChapter as { title?: string } | undefined)
                ?.title ??
              context?.context.currentChapter.title ??
              '故事正文'}
          </span>
          <span>
            {place ?? '旅程进行中'} · 第 {selected ? selected.sequence + 1 : 1}{' '}
            页
          </span>
        </div>
        <article className="story-scroll" ref={article}>
          {error && (
            <Notice
              text={error}
              retry={() =>
                selected ? void selectBranch(selected.id) : void load()
              }
            />
          )}
          {loading || (writing && !streamText) ? (
            <Loading text={writing ? '正在续写…' : '正在读取这一页…'} />
          ) : selected ? (
            <div
              className={`story-column ${writing ? 'streaming-prose' : ''}`}
              aria-busy={writing}
            >
              <StoryProse
                key={selected.id}
                text={readingText}
                sessionId={sessionId}
                branchId={selected.id}
                title={storyTitle}
                place={place ?? undefined}
                opening={selected.kind === 'source_entry'}
                streaming={writing}
              />
              {!readingText.trim() && (
                <p className="muted">
                  这一页暂时没有正文，可以选择一个方向继续。
                </p>
              )}
              <div className="page-turner">
                <button
                  className="text-button"
                  disabled={!parent || writing}
                  onClick={() => {
                    if (parent) {
                      setNotice('')
                      void selectBranch(parent.id)
                    }
                  }}
                >
                  上一页
                </button>

                <button
                  className="text-button"
                  disabled={!children.length || writing}
                  onClick={() => {
                    setNotice('')
                    void selectBranch(children[0].id)
                  }}
                >
                  下一页
                </button>
              </div>
            </div>
          ) : (
            <div className="empty-state">
              <p>还没有正文，请从书架重新开始。</p>
              <Link to="/packages" className="button primary">
                返回书架
              </Link>
            </div>
          )}
        </article>
        {selected && (
          <section className="choice-dock" aria-label="选择故事方向">
            <div className="choice-dock-inner">
              <div className="divider">
                <span>{writing ? '续写中…' : '你的行动'}</span>
              </div>
              {routeEnded ? (
                <div className="route-ending">
                  <h2>
                    {journal.status === 'completed'
                      ? '这段故事，走到了结局'
                      : '你在这里停下了脚步'}
                  </h2>
                  <p>
                    这条路线已保存。你可以在目标时间轴中回到更早的选择，尝试另一种可能。
                  </p>
                  <button
                    className="button subtle"
                    onClick={() => {
                      setTab('goal')
                      setRailOpen(true)
                    }}
                  >
                    回顾这段经历
                  </button>
                </div>
              ) : reviewing ? (
                <div className="rewind-choice">
                  <p>你正在回看之前的故事。</p>
                  <button
                    className="button primary"
                    disabled={writing || loading}
                    onClick={() => setRechoosing(true)}
                  >
                    从这里重新选择
                  </button>
                  <small>新的选择会开启另一条分支，原路线仍然保留。</small>
                </div>
              ) : playable ? (
                <>
                  {!!journal?.feedback.length && !writing && (
                    <div className="choice-feedback">
                      {journal.feedback.map((f, i) => (
                        <span key={i}>{f}</span>
                      ))}
                    </div>
                  )}
                  {rechoosing && (
                    <p className="muted">下一次行动将开启新的分支。</p>
                  )}
                  <div className="direction-list">
                    {choices.map((d, i) => (
                      <button
                        className="direction"
                        key={d.key}
                        title={d.summary ?? undefined}
                        disabled={writing || loading}
                        onClick={() => submit(d.payload)}
                      >
                        <span>{String(i + 1).padStart(2, '0')}</span>
                        <span className="direction-copy">
                          <strong>{d.title}</strong>
                          <small>{d.summary}</small>
                        </span>
                      </button>
                    ))}
                  </div>
                  <form
                    className="free-action"
                    onSubmit={(e) => {
                      e.preventDefault()
                      if (freeText.trim()) submit({ text: freeText.trim() })
                    }}
                  >
                    <textarea
                      aria-label="自由行动"
                      placeholder="写下你的行动…"
                      value={freeText}
                      maxLength={2000}
                      rows={2}
                      disabled={writing || loading}
                      onChange={(e) => setFreeText(e.target.value)}
                    />
                    <button
                      type="submit"
                      className="button primary"
                      disabled={!freeText.trim() || writing || loading}
                    >
                      续写
                    </button>
                  </form>
                  {writing ? (
                    <div className="generation-status" role="status">
                      <span className="loader" />
                      正在续写 · {elapsed} 秒
                    </div>
                  ) : null}
                </>
              ) : (
                <p className="muted">
                  当前可以阅读已保存的故事，续写服务暂未开启。
                </p>
              )}
              {notice && (
                <div className="notice" role="alert">
                  <span>{notice}</span>
                  {!!streamText && !writing && (
                    <details className="unfinished-prose">
                      <summary>查看未完成的片段</summary>
                      <p>{streamText}</p>
                    </details>
                  )}
                  {retryable && attempt.current && (
                    <button
                      disabled={writing}
                      className="button subtle"
                      onClick={() =>
                        attempt.current && void runTurn(attempt.current)
                      }
                    >
                      重试本回合
                    </button>
                  )}
                </div>
              )}
            </div>
          </section>
        )}
      </section>
      <dialog
        ref={endDialog}
        className="save-dialog"
        aria-label="结束路线"
        onCancel={() => setConfirmEnd(false)}
      >
        <h2>在这里结束这条路线？</h2>
        <p>已读内容会保留，你仍可回到更早的选择开启新分支。</p>
        <div className="dialog-actions">
          <button
            className="button subtle"
            disabled={ending}
            onClick={() => setConfirmEnd(false)}
          >
            继续探索
          </button>
          <button
            className="button primary"
            disabled={ending}
            onClick={async () => {
              if (!selected) return
              setEnding(true)
              try {
                await api.endRoute(sessionId, selected.id)
                setJournal(await api.journey(sessionId, selected.id))
                setConfirmEnd(false)
                setRailOpen(false)
              } catch {
                setNotice('未能结束路线，请重试。')
              } finally {
                setEnding(false)
              }
            }}
          >
            结束并保存
          </button>
        </div>
      </dialog>
      <dialog
        ref={notes}
        className={`story-rail ${railOpen ? 'open' : ''}`}
        aria-label="故事手记"
        onCancel={() => setRailOpen(false)}
      >
        <div className="rail-cover">
          <button
            className="rail-close"
            aria-label="关闭故事手记"
            onClick={() => setRailOpen(false)}
          >
            ×
          </button>
          <img src={storyImage(storyTitle)} alt="故事氛围插画" />
          <div>
            <h2>故事手记</h2>
          </div>
        </div>
        <div className="rail-tabs" role="tablist" aria-label="故事手记">
          {(
            [
              ['goal', '目标'],
              ['people', '人物'],
              ['clues', '线索'],
            ] as const
          ).map(([id, label]) => (
            <button
              role="tab"
              aria-selected={tab === id}
              key={id}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="rail-body" role="tabpanel">
          {tab === 'goal' ? (
            <div className="goal-journal">
              <span className="eyebrow">你的目标</span>
              <h3>{journal?.goal || '探索眼前的故事'}</h3>
              {journal && (
                <>
                  <div className="goal-progress">
                    <span>{journal.progress_label}</span>
                    <strong>{journal.progress}%</strong>
                  </div>
                  <progress
                    max={100}
                    value={journal.progress}
                    aria-label="路线阶段进度"
                  />
                  <p>{journal.current_task}</p>
                  <ol className="choice-timeline" aria-label="你的选择时间轴">
                    {branches.map((b) => {
                      const onRoute = journal.lineage.includes(b.id)
                      const fork = branches.filter((other) => other.parent_id === b.parent_id).length > 1
                      return (
                        <li key={b.id} className={`${onRoute ? 'on-route' : 'other-route'} ${selected?.id === b.id ? 'current' : ''}`}>
                          <button
                            disabled={writing || loading}
                            aria-current={selected?.id === b.id ? 'step' : undefined}
                            onClick={() => {
                              setNotice('')
                              void selectBranch(b.id)
                              setRailOpen(false)
                            }}
                          >
                            <span>第 {b.sequence + 1} 页{selected?.id === b.id ? ' · 当前' : ''}</span>
                            <strong>{b.parent_id ? b.action || '继续探索故事' : '故事开篇'}</strong>
                            {fork && b.parent_id && <small>从第 {(branches.find((p) => p.id === b.parent_id)?.sequence ?? 0) + 1} 页作出的{onRoute ? '选择' : '另一种选择'}</small>}
                            {journal.recap.find((step) => step.branch_id === b.id)?.effects.map((effect) => <small key={effect}>{effect}</small>)}
                          </button>
                        </li>
                      )
                    })}
                  </ol>
                </>
              )}
              {playable && !routeEnded && !children.length && (
                <button
                  className="text-button end-route"
                  disabled={writing || loading}
                  onClick={() => setConfirmEnd(true)}
                >
                  在此结束这条路线
                </button>
              )}
            </div>
          ) : tab === 'people' ? (
            <>
              <span className="eyebrow">此刻的相遇</span>
              {!!people.length && <CharacterGraph key={selected?.id} people={people}
                relationships={journal?.relationships ?? []} onSelect={(person) => void openPerson(person)} />}
              {!people.length && (
                <p className="rail-empty">
                  {contextError || '人物会随着你的阅读逐渐出现在这里。'}
                </p>
              )}
            </>
          ) : (
            <>
              <span className="eyebrow">尚待解开的事</span>
              {journal?.clues.map((t, i) => (
                <p className="clue-note" key={i}>
                  {t}
                </p>
              ))}
              {!journal?.clues.length && (
                <p className="rail-empty">
                  目前没有记录到线索，继续探索眼前的故事。
                </p>
              )}
            </>
          )}
        </div>
      </dialog>
      <dialog
        ref={personDialog}
        className="save-dialog character-memory-dialog"
        aria-label={personCard ? `${personCard.name}的人物卡` : '人物卡'}
        onCancel={closePerson}
      >
        {personCard && <>
          <button className="rail-close" aria-label="关闭人物卡" onClick={closePerson}>×</button>
          <div className="character-memory-heading">
            <span className="character-avatar avatar-0">{personCard.name.slice(0, 1)}</span>
            <div><span className="eyebrow">{personCard.is_player ? '你的身份' : '故事中的相遇'}</span><h2>{personCard.name}</h2></div>
          </div>
          <p className="character-identity">{personCard.identity}</p>
          <div className="character-profile-summary" aria-busy={profileLoading}>
            {profileLoading ? <Loading text="正在整理人物概况…" /> : <p>{personCard.summary}</p>}
          </div>
          {profileError && <button className="text-button" onClick={() => void openPerson(personCard)}>重新整理概况</button>}
        </>}
      </dialog>
    </main>
  )
}
