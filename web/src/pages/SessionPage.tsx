import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { prepareOpeningImage } from '../components/openingImage'
import { StoryProse } from '../components/StoryProse'
import { RouteClosurePanel } from '../components/RouteClosurePanel'
import { CharacterGraph } from '../components/CharacterGraph'
import { GraphPortrait } from '../components/GraphPortrait'
import { api } from '../api/client'
import { createReadingTiming, saveReadingTiming } from '../components/readingTimings'
import { observeReadingDisplay } from '../components/readingDisplay'
import { subscribeChoices } from '../components/choiceSubscription'
import type {
  OpeningNavigation,
  PublishedScene,
  PreparedChoice,
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

function playerFacingAction(value?: string | null) {
  const raw = (value ?? '').trim().replace(/^自定行动[：:]\s*/, '')
  if (!raw) return '继续探索故事'
  return raw.replaceAll('我', '你')
}

export function SessionPage() {
  const { sessionId = '' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const opening = useRef((sessionId === 'new' ? location.state?.opening : null) as OpeningNavigation | null).current
  const [openingImage, setOpeningImage] = useState<PublishedScene | null>(null)
  const [openingBranch, setOpeningBranch] = useState<string | null>(null)
  const openingLock = useRef(false)
  const [restoredImage, setRestoredImage] = useState<{ branch: string; image: PublishedScene | null } | null>(null)
  const imageController = useRef<AbortController | null>(null)
  const [session, setSession] = useState<SessionView | null>(null)
  const [catalog, setCatalog] = useState<PackageCatalog | null>(opening?.catalog ?? null)
  const [branches, setBranches] = useState<BranchSummaryItem[]>([])
  const [selected, setSelected] = useState<BranchView | null>(null)
  const [chapter, setChapter] = useState<SourceChapterView | null>(null)
  const [state, setState] = useState<StateView | null>(null)
  const [journal, setJournal] = useState<Journey | null>(null)
  const [journalError, setJournalError] = useState('')
  const [historyId, setHistoryId] = useState<string | undefined>()
  const rechoosing = historyId !== undefined
  const [context, setContext] = useState<ContextView | null>(null)
  const [contextError, setContextError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [writing, setWriting] = useState(false)
  const [streamText, setStreamText] = useState('')
  const streamController = useRef<AbortController | null>(null)
  const replayController = useRef<AbortController | null>(null)
  const [playable, setPlayable] = useState(false)
  const [choices, setChoices] = useState<PreparedChoice[]>([])
  const [choicesError, setChoicesError] = useState('')
  const [choicesLoaded, setChoicesLoaded] = useState(false)
  const [choicesParent, setChoicesParent] = useState<string | null>(null)
  const readingTiming = useRef<ReturnType<typeof createReadingTiming> | null>(null)
  const choiceSubscription = useRef<string | null>(null)
  const [freeText, setFreeText] = useState('')
  const [tab, setTab] = useState<'goal' | 'review' | 'people' | 'clues'>(
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
  function replaySavedText(text: string, seq: number) {
    replayController.current?.abort()
    const controller = new AbortController()
    replayController.current = controller
    const characters = Array.from(text)
    if (!characters.length) return Promise.resolve()
    setWriting(true)
    setStreamText('')
    return new Promise<void>((resolve) => {
      let offset = 0
      let timer = 0
      const finish = () => {
        window.clearInterval(timer)
        controller.signal.removeEventListener('abort', finish)
        if (seq === selecting.current && !controller.signal.aborted) {
          setStreamText('')
          setWriting(false)
        }
        resolve()
      }
      controller.signal.addEventListener('abort', finish, { once: true })
      timer = window.setInterval(() => {
        if (seq !== selecting.current || controller.signal.aborted) {
          finish()
          return
        }
        offset = Math.min(characters.length, offset + 24)
        setStreamText(characters.slice(0, offset).join(''))
        if (offset >= characters.length) finish()
      }, 18)
    })
  }
  async function selectBranch(id: string, readyBranch?: BranchView, preserveReading = false) {
    if (!preserveReading) {
      readingTiming.current?.finish('interrupted')
      readingTiming.current = null
    }
    const seq = ++selecting.current
    imageController.current?.abort()
    replayController.current?.abort()
    if (!preserveReading) setLoading(true)
    setJournal(null)
    setJournalError('')
    closePerson()
    setHistoryId(undefined)
    setError('')
    setContext(null)
    setContextError('')
    setState(null)
    setChapter(null)
    setRetryable(false)
    attempt.current = null
    try {
      const branch = readyBranch ?? await api.getBranch(sessionId, id)
      if (seq !== selecting.current) return
      if (!branch.parentId && !preserveReading) {
        // Revisiting an opening follows the same image-first decision, using
        // the saved branch's compatibility check rather than the source future.
        const controller = new AbortController()
        imageController.current = controller
        const timeout = window.setTimeout(() => controller.abort(), 1500)
        let image: PublishedScene | null = null
        try {
          const art = await api.viewIllustrations(sessionId, id, controller.signal)
          const asset = art.items.find((i) => i.source === 'published' && i.status === 'ready' && i.url)
          if (asset?.url) image = await prepareOpeningImage({ id: asset.url, url: asset.url,
            alt: asset.alt, source: 'published' }, controller.signal)
        } catch { /* A missing image never blocks reading. */ }
        finally { window.clearTimeout(timeout) }
        if (seq !== selecting.current) return
        setRestoredImage({ branch: id, image })
      }
      setSelected(branch)
      setLoading(false)
      if (!preserveReading && branch.narrativeText?.trim()) {
        await replaySavedText(branch.narrativeText, seq)
      }
      if (branch.canonicalRelation === 'on_line' && !branch.narrativeText?.trim() && !preserveReading) {
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
      void loadJournal(id, seq)
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
      if (!preserveReading) requestAnimationFrame(() => {
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
  async function loadJournal(id: string, seq = selecting.current) {
    setJournal(null)
    setJournalError('')
    try {
      const result = await api.journey(sessionId, id)
      if (seq !== selecting.current) return
      if (result.branch_id !== id) throw new Error('手记与当前分支不一致')
      setJournal(result)
    } catch {
      if (seq === selecting.current) setJournalError('这一页的手记暂未加载成功，正文仍可阅读。')
    }
  }
  function reviewBranch(id: string, rechoose = false) {
    setNotice('')
    void selectBranch(id).then(() => {
      if (rechoose) {
        setHistoryId(crypto.randomUUID())
        setTab('goal')
      }
      setRailOpen(false)
    })
  }
  async function startOpening() {
    if (!opening || openingLock.current) return
    openingLock.current = true
    beginReadingTiming(opening.request.request_id, 'opening')
    setError('')
    setWriting(true)
    setStreamText('')
    const controller = new AbortController()
    streamController.current = controller
    try {
      const entry = opening.catalog.entries.find((e) => e.id === opening.request.entry_point_id)
      const image = await prepareOpeningImage(entry?.opening_image, controller.signal)
      if (controller.signal.aborted) return
      setOpeningImage(image)
      const result = await api.streamOpening(opening.request,
        (text) => setStreamText((previous) => previous + text),
        () => setStreamText(''), controller.signal)
      if (controller.signal.aborted) return
      readingTiming.current?.confirm(result.branch.id, result.session.id)
      setSelected(result.branch)
      setOpeningBranch(result.branch.id)
      setLoading(false)
      setStreamText('')
      navigate(`/sessions/${result.session.id}`, { replace: true, state: {
        openingKey: opening.request.request_id, focus: result.branch.id, preserveScroll: true,
      } })
    } catch {
      readingTiming.current?.finish(controller.signal.aborted ? 'interrupted' : 'failed')
      if (!controller.signal.aborted) setError('开场尚未完成，请重试。重试会恢复同一次开局。')
    } finally {
      openingLock.current = false
      if (!controller.signal.aborted) setWriting(false)
    }
  }
  async function load() {
    if (sessionId === 'new') { void startOpening(); return }
    const preserveReading = !!openingBranch && selected?.id === openingBranch
    setError('')
    if (!preserveReading) setLoading(true)
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
      if (initial) await selectBranch(initial.id, preserveReading && selected?.id === initial.id ? selected : undefined, preserveReading)
      else setLoading(false)
      if (focus) navigate(location.pathname, { replace: true, state: {
        ...location.state, focus: undefined, preserveScroll: true,
      } })
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
      imageController.current?.abort()
    }
  }, [sessionId])
  // Opening completion replaces /sessions/new inside the same reader. Only
  // leaving this reader interrupts its clock; the internal handoff does not.
  useEffect(() => () => readingTiming.current?.finish('interrupted'), [])
  useEffect(() => {
    if (!writing) return
    setElapsed(0)
    const timer = window.setInterval(() => setElapsed((t) => t + 1), 1000)
    return () => window.clearInterval(timer)
  }, [writing])
  // Only visible, actionable cards subscribe. Every card uses the server's
  // stable choice identity; polling renews the lease without new model calls.
  const choicesVisible = !!selected && !!journal && journal.status === 'active' && playable && !loading &&
    (!branches.some((b) => b.parent_id === selected.id) || rechoosing)
  useEffect(() => {
    setChoices([])
    setChoicesError('')
    setChoicesLoaded(false)
    setChoicesParent(null)
    if (!choicesVisible || !selected) return
    const parent = selected.id
    const prefetched = new Set<string>()
    return subscribeChoices(parent, {
      prepare: subscriber => api.prepareChoices(sessionId, parent, subscriber, historyId),
      release: subscriber => api.releaseChoices(sessionId, parent, subscriber),
      subscription: subscriber => { choiceSubscription.current = subscriber },
      locked: () => locked.current,
      ready: result => {
        setChoices(result.choices)
        setChoicesParent(parent)
        setChoicesLoaded(true)
        for (const choice of result.choices) {
          if (choice.image_prefetch_url && !prefetched.has(choice.image_prefetch_url)) {
            prefetched.add(choice.image_prefetch_url)
            const image = new Image()
            image.src = choice.image_prefetch_url
          }
        }
        setChoicesError('')
      },
      error: () => setChoicesError('方向暂未准备好，你仍可以填写自己的行动。'),
    })
  }, [sessionId, selected?.id, choicesVisible, historyId])
  function beginReadingTiming(requestId: string, kind: 'opening' | 'turn') {
    readingTiming.current?.finish('interrupted')
    readingTiming.current = createReadingTiming({
      attempt_id: crypto.randomUUID(), request_id: requestId, session_id: sessionId, kind,
    }, (record) => {
      try { saveReadingTiming(record, sessionStorage) } catch { /* Storage can be disabled. */ }
      console.debug('[story-reading]', JSON.stringify(record))
    })
  }
  async function runTurn(payload: Attempt) {
    if (locked.current) return
    locked.current = true
    beginReadingTiming(payload.request_id, 'turn')
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
        readingTiming.current?.finish('failed')
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
                action: payload.text || selected?.nextDirections.find((d) => d.id === payload.direction_id)?.title || selected?.nextDirections.find((d) => d.id === payload.direction_id)?.summary || branch.summary,
              },
            ],
      )
      readingTiming.current?.confirm(branch.id)
      await selectBranch(branch.id, branch, !result.deduplicated)
      setStreamText('')
      allBranches()
        .then(setBranches)
        .catch(() =>
          setNotice('正文已保存，阅读记录刷新失败。重新打开故事即可恢复。'),
        )
      if (result.deduplicated) setNotice('已恢复上次生成的内容。')
    } catch (e) {
      readingTiming.current?.finish('failed')
      setNotice('这次续写尚未确认，故事仍停在上一页。你可以重试本回合。')
      setRetryable(true)
    } finally {
      locked.current = false
      setWriting(false)
    }
  }
  function submit(payload: { direction_id?: string; text?: string; choice_id?: string; draft_id?: string }) {
    if (!selected || locked.current || loading) return
    const next = {
      parent_branch_id: selected.id,
      request_id: crypto.randomUUID(),
      subscriber_id: choiceSubscription.current ?? undefined,
      history_id: historyId,
      ...payload,
    }
    attempt.current = next
    void runTurn(next)
  }
  const currentChoices = choicesParent === selected?.id ? choices : []
  const readingText = writing
    ? streamText || chapter?.text || selected?.narrativeText || ''
    : (chapter?.text ?? selected?.narrativeText ?? '')
  useEffect(() => {
    if (!playable || loading || writing || sessionId === 'new' || !selected?.parentId ||
        !selected.narrativeText?.trim() || !article.current) return
    const branchId = selected.id
    return observeReadingDisplay(article.current, () => api.recordReadingDisplay(sessionId, branchId))
  }, [playable, loading, writing, sessionId, selected?.id, selected?.parentId, selected?.narrativeText])
  useEffect(() => {
    readingTiming.current?.rendered(readingText, writing, selected?.id,
      choicesVisible && choicesParent === selected?.id && currentChoices.length > 0)
  }, [readingText, writing, selected?.id, choicesVisible, choicesParent, currentChoices.length])
  if (!session && !opening)
    return (
      <main className="page">
        {sessionId === 'new' ? <Link to="/packages">请从书架选择身份开始故事</Link> : error ? <Notice text={error} retry={load} /> : <Loading />}
      </main>
    )
  const storyTitle = catalog?.package.title ?? '我的故事'
  const currentState = state?.state ?? selected?.branchState
  const roleId = currentState?.playerCharacterId ?? opening?.request.source_character_id
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
  const activeGoals = journal?.goals?.filter((goal) => goal.status === 'active') ?? []
  const activeGoalTitles = new Set(activeGoals.map((goal) => goal.title))
  const openThreads = journal?.threads?.filter((thread) => thread.status === 'open' && !activeGoalTitles.has(thread.title)) ?? []
  const reviewing = children.length > 0 && !rechoosing
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
            disabled={!selected}
            aria-expanded={railOpen}
            onClick={() => setRailOpen(!railOpen)}
          >
            故事手记
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
          {(loading || (writing && !streamText)) && !opening ? (
            <Loading text={writing ? '正在续写…' : '正在读取这一页…'} />
          ) : selected || opening ? (
            <div
              className={`story-column ${writing ? 'streaming-prose' : ''}`}
              aria-busy={writing}
            >
              <StoryProse
                key={opening && (!selected || selected.id === openingBranch) ? opening.request.request_id : selected?.id}
                text={readingText}
                sessionId={sessionId}
                branchId={selected?.id}
                title={storyTitle}
                place={place ?? undefined}
                opening={!selected || selected.kind === 'source_entry'}
                fixedOpeningImage={opening && (!selected || selected.id === openingBranch) && (sessionId === 'new' || !writing) ? openingImage : !writing && restoredImage?.branch === selected?.id ? restoredImage?.image : undefined}
                streaming={writing}
              />
              {sessionId === 'new' && writing && !streamText && <Loading text="故事正在展开…" />}
              {selected && (parent || children.length > 0) && <div className="page-turner">
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
              </div>}
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
              {writing ? (
                <div className="generation-status" role="status">
                  <span className="loader" />
                  正在续写 · {elapsed} 秒
                </div>
              ) : routeEnded ? (
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
                    onClick={() => setHistoryId(crypto.randomUUID())}
                  >
                    重选
                  </button>
                  <small>新的选择会开启另一条分支，原路线仍然保留。</small>
                </div>
              ) : playable ? (
                <>
                  {journalError && <Notice text={journalError} retry={() => selected && void loadJournal(selected.id)} />}
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
                  {choicesError && <p className="muted">{choicesError}</p>}
                  {!currentChoices.length && !choicesError && !journalError && !choicesLoaded && <p className="muted">正在整理眼前的选择…</p>}
                  <div className={`direction-list ${currentChoices.length === 2 ? 'two-choice' : ''}`}>
                    {currentChoices.map((d, i) => (
                      <button
                        className="direction"
                        key={d.id}
                        title={d.summary ?? undefined}
                        disabled={writing || loading}
                        onClick={() => submit({ choice_id: d.id, draft_id: d.draft_id })}
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
                    <details className="unfinished-prose" open>
                      <summary>未确认草稿 · 未计入故事进展</summary>
                      {streamText.split(/\n\s*\n/).filter(Boolean).map((paragraph, i) => <p key={i}>{paragraph}</p>)}
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
              ['review', '回顾'],
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
          {loading || !journal || journal.branch_id !== selected?.id ? (
            journalError && !loading ? <Notice text={journalError} retry={() => selected && void loadJournal(selected.id)} />
              : <Loading text="正在读取这一页的手记…" />
          ) : tab === 'goal' ? (
            <div className="goal-journal">
              <div className="journal-meta" aria-label="阅读状态">
                <span>{selected ? `第 ${selected.sequence + 1} 页` : '故事开篇'}</span>
                <span>{routeEnded ? '路线已结束' : '故事进行中'}</span>
              </div>
              <section className="journal-section journal-goal" aria-label="当前目标">
                <span className="eyebrow">当前目标</span>
                {activeGoals.length ? <ul className="journal-goal-list">
                  {activeGoals.slice(0, 4).map((goal) => <li key={goal.id}>
                    <strong>{goal.title}</strong>
                    {goal.reason && <small>{goal.reason}</small>}
                  </li>)}
                </ul> : <p className="journal-empty">当前没有进行中的目标。</p>}
              {!!journal.current_task && <section className="journal-section journal-current-task" aria-label="眼下任务">
                <span className="eyebrow">眼下任务</span>
                <p>{journal.current_task}</p>
              </section>}
              </section>
              {!!openThreads.length && <section className="journal-section" aria-label="待关注问题">
                <span className="eyebrow">还要留意</span>
                {!!openThreads.length ? <ul className="journal-attention-list">
                  {openThreads.slice(0, 4).map((thread) => <li key={thread.id}>{thread.title}</li>)}
                </ul> : null}
              </section>}
              {!!journal.goals?.some((goal) => goal.status !== 'active') && (
                <details className="journal-history">
                  <summary>查看目标变化</summary>
                  <ul aria-label="目标变化">
                    {journal.goals.filter((goal) => goal.status !== 'active').map((goal) => (
                      <li key={goal.id}>
                        <strong>{{ completed: '已完成', transformed: '已转化', abandoned: '已放下', active: '进行中' }[goal.status]} · {goal.title}</strong>
                        {goal.reason && <p>{goal.reason}</p>}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {selected && <details className="journal-advanced">
                <summary>{routeEnded ? '查看路线收束记录' : '路线设置与结束'}</summary>
                <RouteClosurePanel
                  sessionId={sessionId}
                  branchId={selected.id}
                  narrative={selected.narrativeText}
                  canEdit={playable && !routeEnded && children.length === 0}
                  onChanged={() => void loadJournal(selected.id)}
                />
              </details>}
            </div>
          ) : tab === 'review' ? (
            <section className="journal-review" aria-label="故事回顾">
              <span className="eyebrow">故事回顾</span>
              <ol className="journal-review-list">
                  {branches.map((b) => {
                    const onRoute = journal.lineage.includes(b.id)
                    const fork = branches.filter((other) => other.parent_id === b.parent_id).length > 1
                    const currentCard = selected?.id === b.id
                    const openCard = () => {
                      if (!currentCard && !writing && !loading) reviewBranch(b.id)
                    }
                    return (
                      <li key={b.id} className={`${onRoute ? 'on-route' : 'other-route'} ${currentCard ? 'current' : ''} ${currentCard ? '' : 'clickable'}`}
                        onClick={(event) => {
                          if ((event.target as HTMLElement).closest('button')) return
                          openCard()
                        }}
                        onKeyDown={(event) => {
                          if ((event.key === 'Enter' || event.key === ' ') && !currentCard) {
                            event.preventDefault()
                            openCard()
                          }
                        }}
                        role={currentCard ? undefined : 'button'} tabIndex={currentCard ? undefined : 0}>
                        <div className="journal-review-entry">
                          <div className="journal-review-copy">
                            <span>第 {b.sequence + 1} 页{selected?.id === b.id ? ' · 当前' : ''}</span>
                            <strong>{b.parent_id ? playerFacingAction(b.action) : '故事开篇'}</strong>
                            {fork && b.parent_id && <small>{onRoute ? '当前路线' : '另一条分支'}</small>}
                          </div>
                          <div className="journal-review-actions">
                            {playable && <button className="text-button" disabled={writing || loading} onClick={() => reviewBranch(b.id, true)}>重选</button>}
                          </div>
                        </div>
                      </li>
                    )
                  })}
              </ol>
            </section>
          ) : tab === 'people' ? (
            <>
              {!!people.length && <CharacterGraph key={selected?.id} people={people}
                relationships={journal?.relationships ?? []} onSelect={(person) => void openPerson(person)} />}
              {!people.length && (
                <p className="rail-empty">
                  {contextError || '暂无人物记录'}
                </p>
              )}
            </>
          ) : (
            <>
              <span className="eyebrow">剧情问题</span>
              {journal?.threads?.map(thread => (
                <div className="clue-note" key={thread.id}>
                  <strong>{thread.title}</strong>
                  <p>{({ open: '待处理', resolved: '已解决', abandoned: '已放下', unknown: '暂未确认' })[thread.status]}</p>
                  {thread.reason && <p>{thread.reason}</p>}
                  {thread.evidence && (
                    <details>
                      <summary>查看正文依据</summary>
                      <p>{thread.evidence}</p>
                    </details>
                  )}
                </div>
              ))}
              {!journal?.threads?.length && <p className="rail-empty">暂无剧情问题</p>}
              <span className="eyebrow">已知线索</span>
              {journal?.clues.map((t, i) => (
                <p className="clue-note" key={i}>
                  {t}
                </p>
              ))}
              {!journal?.clues.length && (
                <p className="rail-empty">
                  暂无线索
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
            <svg className={`character-card-portrait ${personCard.is_player ? 'player' : ''}`}
              data-character-status={personCard.status?.code ?? 'unknown'}
              viewBox="-48 -48 96 96" role="img" aria-label={`${personCard.name}的头像`}>
              <GraphPortrait key={personCard.id} portrait={personCard.portrait} radius={44} />
              <circle className="graph-node-frame" r="44" />
            </svg>
            <div><span className="eyebrow">{personCard.is_player ? '你的身份' : '故事中的相遇'}</span><h2>{personCard.name}</h2>
              <span className="character-status-label">{personCard.status?.label ?? '状态未确认'}
                {personCard.status?.code === 'departed' && personCard.status.permanence && ` · ${personCard.status.permanence === 'permanent' ? '永久离队' : '暂时离队'}`}
              </span>
            </div>
          </div>
          <p className="character-identity">{personCard.identity}</p>
          {personCard.status?.evidence && <div className="character-status-evidence">
            <span>状态依据 · 第 {personCard.status.evidence.page} 页</span>
            <blockquote>{personCard.status.evidence.quote}</blockquote>
          </div>}
          <div className="character-profile-summary" aria-busy={profileLoading}>
            {profileLoading ? <Loading text="正在整理人物概况…" /> : <p>{personCard.summary}</p>}
          </div>
          {profileError && <button className="text-button" onClick={() => void openPerson(personCard)}>重新整理概况</button>}
        </>}
      </dialog>
    </main>
  )
}
