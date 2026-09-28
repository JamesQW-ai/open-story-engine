import { useId } from 'react'

// A local decorative interlude, never presented as a generated story event.
export function ReadingInterlude() {
  const id = useId()
  return <figure className="reading-illustration reading-interlude">
    <svg viewBox="0 0 900 600" width="900" height="600" role="img" aria-label="水墨过渡图，不描绘具体剧情"
      style={{ width: '100%', height: 'auto', display: 'block', borderRadius: 8 }}>
      <defs>
        <linearGradient id={id} x2="0.8" y2="1">
          <stop stopColor="#ded9cb" /><stop offset="1" stopColor="#879b9c" />
        </linearGradient>
      </defs>
      <rect width="900" height="600" fill={`url(#${id})`} />
      <ellipse cx="610" cy="240" rx="300" ry="155" fill="#f2eee1" opacity=".28" />
      <path d="M-60 360 Q120 120 320 305 T940 210 L940 600 H-60Z" fill="#566e71" opacity=".18" />
      <path d="M-60 470 Q170 220 390 420 T960 300 L960 600 H-60Z" fill="#324e55" opacity=".22" />
      <path d="M-20 540 Q210 375 440 470 T920 430 L920 600 H-20Z" fill="#213b45" opacity=".32" />
      <path d="M40 485 Q245 440 410 460 M495 495 Q690 455 835 475" fill="none" stroke="#e9e1ca" strokeWidth="2" opacity=".45" />
    </svg>
    <figcaption className="muted">水墨过渡图 · 正文继续展开</figcaption>
  </figure>
}
