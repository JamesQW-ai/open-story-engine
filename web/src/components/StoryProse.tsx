import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { SceneIllustrations, PublishedScene } from '../api/types'
import { readingSections } from './readingLayout'
import { subscribeIllustration } from './illustrationSubscription'

function SceneImage({ url, alt, shown }: { url: string; alt: string; shown: () => void }) {
  const ref = useRef<HTMLImageElement>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    if (!loaded || !ref.current) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { shown(); observer.disconnect() }
    })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [loaded, shown])
  return <img ref={ref} src={url} alt={alt} onLoad={() => setLoaded(true)}
    loading="eager" decoding="async" width="1536" height="1024" />
}

function illustrationHistoryKey(sessionId: string) {
  return `story-illustrations-used:${sessionId}`
}

function readIllustrationHistory(sessionId: string): Record<string, string> {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(illustrationHistoryKey(sessionId)) ?? '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, string> : {}
  } catch {
    return {}
  }
}

function rememberIllustration(sessionId: string, assetKey: string, branchId: string) {
  try {
    const history = readIllustrationHistory(sessionId)
    history[assetKey] = branchId
    sessionStorage.setItem(illustrationHistoryKey(sessionId), JSON.stringify(history))
  } catch { /* Storage is optional; reading must continue. */ }
}

export function StoryProse({ text, streaming, sessionId, branchId, fixedOpeningImage }: {
  fixedOpeningImage?: PublishedScene | null
  text: string
  title: string
  place?: string
  opening: boolean
  streaming: boolean
  sessionId?: string
  branchId?: string
}) {
  const [art, setArt] = useState<{ branch: string; value: SceneIllustrations } | null>(null)
  const drawRequest = useRef<() => void>(() => undefined)
  const cancelRequest = useRef<() => void>(() => undefined)
  const subscription = useRef('')
  const opened = useRef(0)
  const shown = useRef(false)
  useEffect(() => {
    setArt(null)
    opened.current = 0
    shown.current = false
    if (streaming || !sessionId || !branchId || !text.trim()) return
    const request = subscribeIllustration({
      prepare: (subscriber, draw) => api.illustrations(sessionId, branchId, subscriber, draw),
      release: subscriber => api.releaseIllustrations(sessionId, branchId, subscriber),
      subscription: subscriber => {
        subscription.current = subscriber ?? ''
        if (subscriber) { opened.current = performance.now(); shown.current = false }
      },
      ready: result => setArt({ branch: branchId, value: result }),
      error: () => setArt({ branch: branchId, value: { available: false, can_generate: false,
        items: [{ index: 0, status: 'failed', alt: '', source: 'private' }] } }),
    })
    drawRequest.current = request.draw
    cancelRequest.current = () => {
      request.cancel()
      setArt(previous => previous && previous.branch === branchId ? {
        ...previous, value: { ...previous.value, can_generate: false,
          items: previous.value.items.map(item => ['queued', 'generating'].includes(item.status)
            ? { ...item, status: 'cancelled' } : item) },
      } : previous)
    }
    return () => {
      request.cleanup()
      drawRequest.current = () => undefined
      cancelRequest.current = () => undefined
    }
  }, [sessionId, branchId, streaming])
  const activeArt = !streaming && art && art.branch === branchId ? art.value : null
  const sections = readingSections(text)
  const inlineArt = sections.length > 1
    ? Math.max(0, Math.floor(sections.length / 2) - 1)
    : Math.max(0, sections.length - 1)
  const fixedOpeningKey = fixedOpeningImage?.id || fixedOpeningImage?.url
  const showFixedOpeningImage = Boolean(fixedOpeningImage && (!sessionId || !fixedOpeningKey
    || !readIllustrationHistory(sessionId)[fixedOpeningKey]))
  const imageItems = (activeArt?.items ?? []).filter((image, index, all) => {
    const key = image.id ?? image.url ?? `index:${image.index ?? index}`
    return all.findIndex(candidate => (candidate.id ?? candidate.url ?? `index:${candidate.index}`) === key) === index
  }).filter((image) => {
    // Every image asset is displayed at most once in a reading session.
    // Compatibility decides whether an asset can match; session history
    // decides whether this reader has already seen it.
    const key = image.id ?? image.url
    if (!sessionId || !key) return true
    return !readIllustrationHistory(sessionId)[key]
  })
  const imageBlock = () => <>
    {activeArt?.can_generate && <button className="text-button" onClick={() => drawRequest.current()}>绘制这一幕</button>}
    {imageItems.map((image) => <div key={image.id ?? image.url ?? image.index}>
      {image.status === 'ready' && image.url && <figure className="reading-illustration">
        <SceneImage key={image.url} url={image.url} alt={image.alt} shown={() => {
          if (!shown.current && sessionId && branchId) {
            shown.current = true
            rememberIllustration(sessionId, image.id ?? image.url ?? `index:${image.index}`, branchId)
            void api.shownIllustration(sessionId, branchId, subscription.current, performance.now() - opened.current).catch(() => undefined)
          }
        }} />
      </figure>}
      {(image.status === 'queued' || image.status === 'generating') && <div className="illustration-placeholder" aria-live="polite"><p className="muted">插图正在绘制，你可以继续阅读或行动。</p><button className="text-button" onClick={() => cancelRequest.current()}>取消本页配图</button></div>}
      {image.status === 'cancelled' && <p className="muted">{image.reason === 'deadline' ? '这一幕绘制超时' : '本页配图已停止'}，故事照常继续。</p>}
      {image.status === 'failed' && <p className="muted">这一幕暂未绘成，故事照常继续。</p>}
    </div>)}
  </>
  return <>
    {showFixedOpeningImage && fixedOpeningImage && <figure className="reading-illustration opening-illustration">
      <SceneImage url={fixedOpeningImage.url} alt={fixedOpeningImage.alt} shown={() => {
        if (!shown.current && sessionId && branchId) {
          shown.current = true
          rememberIllustration(sessionId, fixedOpeningImage.id || fixedOpeningImage.url, branchId)
          void api.shownIllustration(sessionId, branchId, subscription.current, performance.now() - opened.current).catch(() => undefined)
        }
      }} />
    </figure>}
    {sections.map((section, i) => <section className="reading-section" key={i} aria-busy={streaming}>
      {section.paragraphs.map((p, j) => <p className="passage" key={j}>{p}</p>)}
      {fixedOpeningImage === undefined && i === inlineArt && imageBlock()}
    </section>)}
    {fixedOpeningImage === undefined && !sections.length && imageBlock()}
  </>
}
