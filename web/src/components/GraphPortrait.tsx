import { useId, useState } from 'react'
import type { JournalPerson } from '../api/types'

const palettes = [
  ['#343d43', '#b3c6c7'], ['#484038', '#d3ba94'],
  ['#3c4540', '#b8c6a6'], ['#493b43', '#cbb1c1'],
  ['#393e50', '#adb8d4'], ['#4b3c35', '#d6b6a4'],
  ['#3b4649', '#a6c9c9'], ['#474536', '#cec59f'],
]

// Neutral emblems distinguish minor characters without inventing their appearance.
export function GraphPortrait({ portrait, radius }: {
  portrait: JournalPerson['portrait'] | undefined
  radius: number
}) {
  const clipId = `portrait-${useId().replace(/:/g, '')}`
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const index = Math.max(0, Math.min(7, Number(portrait?.fallback_key?.match(/^person-([1-8])$/)?.[1] ?? 1) - 1))
  const [background, foreground] = palettes[index]
  const url = portrait?.url
  return <g className="graph-portrait" aria-hidden="true" pointerEvents="none">
    <defs><clipPath id={clipId}><circle r={radius} /></clipPath></defs>
    <g clipPath={`url(#${clipId})`}>
      <g className="graph-portrait-preset" transform={`scale(${radius / 32})`}>
        <circle r="32" fill={background} />
        <path d={index % 2 ? 'M-32 14 L14-32 M-20 32 L32-20' : 'M-32-14 L14 32 M-20-32 L32 20'} stroke={foreground} strokeOpacity=".15" strokeWidth="8" />
        <circle cy="-9" r={index % 3 === 0 ? 10 : 9} fill={foreground} />
        <path d={index % 2 ? 'M-23 33 Q-23 5 0 5 Q23 5 23 33' : 'M-25 33 L-19 14 Q0 3 19 14 L25 33'} fill={foreground} />
        <path d="M-7 10 L0 21 L7 10" fill="none" stroke={background} strokeWidth="2" />
      </g>
      {url && url !== failedUrl && <image key={url} className="graph-portrait-image" href={url}
        x={-radius * 1.2} y={-radius * 1.3} width={radius * 2.7} height={radius * 2.7}
        preserveAspectRatio="xMidYMin slice" onError={() => setFailedUrl(url)} />}
    </g>
  </g>
}
