import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { SceneIllustrations } from '../api/types'
import { readingSections } from './readingLayout'

export function StoryProse({ text, streaming, sessionId, branchId }: {
  text: string
  title: string
  place?: string
  opening: boolean
  streaming: boolean
  sessionId?: string
  branchId?: string
}) {
  const [art, setArt] = useState<SceneIllustrations | null>(null)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    setArt(null)
    if (streaming || !sessionId || !branchId || !text.trim()) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    let failures = 0
    async function poll(generate: boolean, retryFailed = false) {
      try {
        const result = await api.illustrations(sessionId!, branchId!, generate, retryFailed)
        if (cancelled) return
        failures = 0
        setArt(result)
        if (result.available && result.items.some((item) => item.status === 'pending' || item.status === 'idle'))
          timer = setTimeout(() => void poll(result.items.some((item) => item.status === 'idle')), 2500)
      } catch {
        if (!cancelled && ++failures < 3) timer = setTimeout(() => void poll(generate), 4000)
      }
    }
    void poll(true, retry > 0)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [sessionId, branchId, streaming, retry])
  const sections = readingSections(text)
  return <>
    {sections.map((section, i) => {
      const image = art?.items.find((item) => item.index === i)
      return <section className="reading-section" key={i} aria-busy={streaming}>
        {image && <figure className="reading-illustration">
          {image.status === 'ready' && image.url ? <img src={image.url} alt={image.alt}
            loading={i === 0 ? 'eager' : 'lazy'} decoding="async" width="1536" height="1024" />
            : <div className={`scene-art-pending ${image.status === 'failed' ? 'failed' : ''}`}>
              <span>{image.status === 'failed' ? '这一幕暂未绘成' : '这一幕，正在浮现…'}</span>
              {image.status === 'failed' && <button className="text-button" onClick={() => setRetry((r) => r + 1)}>重试插图</button>}
            </div>}
        </figure>}
        {section.paragraphs.map((p, j) => <p className="passage" key={j}>{p}</p>)}
      </section>
    })}
  </>
}
