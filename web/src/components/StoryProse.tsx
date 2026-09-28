import { Fragment, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { SceneIllustrations, PublishedScene } from '../api/types'
import { illustrationAnchor, readingSections } from './readingLayout'
import { subscribeIllustration } from './illustrationSubscription'
import { illustrationAssetKey, readIllustrationHistory, rememberIllustration } from './illustrationHistory'
import { ReadingInterlude } from './ReadingInterlude'

function SceneImage({ url, alt, shown, fallback = false }: { url: string; alt: string; shown: () => void; fallback?: boolean }) {
  const ref = useRef<HTMLImageElement>(null)
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!loaded || !ref.current) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { shown(); observer.disconnect() }
    })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [loaded, shown])
  if (failed) return fallback ? <ReadingInterlude /> : <p className="muted">插图暂未加载，故事照常继续。</p>
  return <img onError={() => setFailed(true)} ref={ref} src={url} alt={alt} onLoad={() => setLoaded(true)}
    loading="eager" decoding="async" width="1536" height="1024" />
}

export function StoryProse({ text, fullText = text, streaming, sessionId, branchId, fixedOpeningImage, intervalIllustration = false }: {
  intervalIllustration?: boolean
  fullText?: string
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
  const visibleAssets = useRef(new Set<string>())
  const defaultAnchor = illustrationAnchor(fullText)
  const hasFixedImage = Boolean(fixedOpeningImage)
  const automatic = defaultAnchor !== null || intervalIllustration
  const fixedInline = hasFixedImage && defaultAnchor !== null
  useEffect(() => {
    setArt(null)
    opened.current = 0
    shown.current = false
    if (!sessionId || !branchId || !fullText.trim() || hasFixedImage) return
    let requested = false
    const request = subscribeIllustration({
      prepare: (subscriber, draw) => api.illustrations(sessionId, branchId, subscriber, draw),
      release: subscriber => api.releaseIllustrations(sessionId, branchId, subscriber),
      subscription: subscriber => {
        subscription.current = subscriber ?? ''
        if (subscriber) { opened.current = performance.now(); shown.current = false }
      },
      ready: result => {
        const shouldDraw = automatic || result.automatic === true
        setArt({ branch: branchId, value: shouldDraw && requested && result.can_generate
          ? { ...result, can_generate: false } : result })
        if (shouldDraw && result.can_generate && !requested) {
          requested = true
          request.draw()
        }
      },
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
  }, [sessionId, branchId, automatic, hasFixedImage])
  const activeArt = art && art.branch === branchId ? art.value : null
  const anchor = illustrationAnchor(fullText, automatic || activeArt?.automatic === true)
  const sections = readingSections(text)
  const fullParagraphs = readingSections(fullText).flatMap(section => section.paragraphs)
  const visibleHere = (key?: string) => {
    if (!key || !sessionId) return true
    if (visibleAssets.current.has(key)) return true
    if (readIllustrationHistory(sessionId)[key]) return false
    visibleAssets.current.add(key)
    return true
  }
  const showFixedOpeningImage = fixedOpeningImage && branchId && visibleHere(illustrationAssetKey(fixedOpeningImage))
  const imageItems = (activeArt?.items ?? []).filter((image, index, all) => {
    const key = illustrationAssetKey(image) ?? `index:${image.index ?? index}`
    return all.findIndex(candidate => (illustrationAssetKey(candidate) ?? `index:${candidate.index}`) === key) === index
  }).filter((image) => {
    // Every image asset is displayed at most once in a reading session.
    // Compatibility decides whether an asset can match; session history
    // decides whether this reader has already seen it.
    const key = illustrationAssetKey(image)
    if (!sessionId || !key) return true
    return visibleHere(key)
  })
  const fixedImage = () => showFixedOpeningImage && fixedOpeningImage ? <figure className="reading-illustration opening-illustration">
    <SceneImage url={fixedOpeningImage.url} alt={fixedOpeningImage.alt} fallback={automatic} shown={() => {
      if (!shown.current && sessionId && branchId) {
        shown.current = true
        const key = illustrationAssetKey(fixedOpeningImage)
        if (key) rememberIllustration(sessionId, key, branchId)
        void api.viewIllustrations(sessionId, branchId, new AbortController().signal).catch(() => undefined)
        void api.shownIllustration(sessionId, branchId, subscription.current, performance.now() - opened.current, 'published').catch(() => undefined)
      }
    }} />
  </figure> : automatic ? <ReadingInterlude /> : null
  const imageBlock = () => fixedInline ? fixedImage() : <>
    {automatic && !imageItems.some(image => image.status === 'ready' && image.url) && <ReadingInterlude />}
    {activeArt?.can_generate && !(automatic || activeArt.automatic) && <button className="text-button" onClick={() => drawRequest.current()}>绘制这一幕</button>}
    {imageItems.map((image) => <div key={image.id ?? image.url ?? image.index}>
      {image.status === 'ready' && image.url && <figure className="reading-illustration">
        <SceneImage key={image.url} url={image.url} alt={image.alt} fallback={automatic} shown={() => {
          if (!shown.current && sessionId && branchId) {
            shown.current = true
            const key = illustrationAssetKey(image)
            if (key) rememberIllustration(sessionId, key, branchId)
            void api.shownIllustration(sessionId, branchId, subscription.current, performance.now() - opened.current, image.source).catch(() => undefined)
          }
        }} />
      </figure>}
      {(image.status === 'queued' || image.status === 'generating') && <div aria-live="polite"><p className="muted">插图正在绘制，你可以继续阅读或行动。</p><button className="text-button" onClick={() => cancelRequest.current()}>取消本页配图</button></div>}
      {image.status === 'cancelled' && <p className="muted">{image.reason === 'deadline' ? '这一幕绘制超时' : '本页配图已停止'}，故事照常继续。</p>}
      {image.status === 'failed' && <p className="muted">这一幕暂未绘成，故事照常继续。</p>}
    </div>)}
  </>
  return <>
    {!fixedInline && hasFixedImage && fixedImage()}
    {sections.map((section, i) => <section className="reading-section" key={i} aria-busy={streaming}>
      {section.paragraphs.map((p, j) => {
        const index = sections.slice(0, i).reduce((count, item) => count + item.paragraphs.length, 0) + j
        return <Fragment key={j}>
          <p className="passage">{p}</p>
          {(!hasFixedImage || fixedInline) && index === anchor && p === fullParagraphs[index] && <div className="inline-scene-slot">{imageBlock()}</div>}
        </Fragment>
      })}
      {!hasFixedImage && anchor === null && !streaming && i === sections.length - 1 && imageBlock()}
    </section>)}
  </>
}
