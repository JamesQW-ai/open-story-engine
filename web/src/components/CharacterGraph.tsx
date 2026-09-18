import { useMemo, useRef, useState } from 'react'
import type { JournalPerson, JournalRelationship } from '../api/types'
import { relationshipLayout } from './relationshipLayout'
import { GraphPortrait } from './GraphPortrait'
import { relationshipLabelPosition } from './relationshipGeometry'

export function CharacterGraph({ people, relationships, onSelect }: {
  people: JournalPerson[]
  relationships: JournalRelationship[]
  onSelect: (person: JournalPerson) => void
}) {
  const layout = useMemo(() => relationshipLayout(people, relationships), [people, relationships])
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({})
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 })
  const [focus, setFocus] = useState<string | null>(null)
  const drag = useRef<{ id?: string; x: number; y: number; moved: boolean } | null>(null)
  const svg = useRef<SVGSVGElement>(null)
  const point = (node: typeof layout.nodes[number]) => positions[node.id] ?? node
  const nodes = new Map(layout.nodes.map((node) => [node.id, node]))
  const adjacent = new Set(layout.edges.filter((edge) => edge.source === focus || edge.target === focus).flatMap((edge) => [edge.source, edge.target]))
  function zoom(delta: number) { setView((v) => ({ ...v, zoom: Math.max(.6, Math.min(2, v.zoom + delta)) })) }
  return <div className="character-graph">
    <div className="graph-tools">
      <span>{people.length} 位人物 · {layout.edges.length} 条联系</span>
      <div><button aria-label="缩小关系图" onClick={() => zoom(-.2)}>−</button><button aria-label="放大关系图" onClick={() => zoom(.2)}>+</button>
        <button aria-label="重置关系图" onClick={() => { setView({ x: 0, y: 0, zoom: 1 }); setPositions({}) }}>复位</button></div>
    </div>
    <svg ref={svg} viewBox="0 0 420 420" role="group" aria-label="人物关系图，可拖动人物，点击查看身份卡"
      onPointerMove={(event) => {
        const current = drag.current
        if (!current) return
        const scale = 420 / (svg.current?.getBoundingClientRect().width || 420)
        const dx = (event.clientX - current.x) * scale, dy = (event.clientY - current.y) * scale
        if (Math.abs(dx) + Math.abs(dy) < 2) return
        current.moved = true
        current.x = event.clientX; current.y = event.clientY
        if (current.id) {
          const id = current.id, origin = point(nodes.get(id)!)
          setPositions((p) => ({ ...p, [id]: { x: origin.x + dx / view.zoom, y: origin.y + dy / view.zoom } }))
        } else setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }))
      }}
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) {
          drag.current = { x: event.clientX, y: event.clientY, moved: false }
          event.currentTarget.setPointerCapture(event.pointerId)
        }
      }}
      onPointerUp={() => { if (!drag.current?.id) drag.current = null }}
      onPointerCancel={() => { drag.current = null }}>
      <g transform={`translate(${210 + view.x} ${205 + view.y}) scale(${view.zoom}) translate(-210 -205)`}>
        {layout.edges.map((edge) => {
          const a = point(nodes.get(edge.source)!), b = point(nodes.get(edge.target)!)
          const active = !focus || edge.source === focus || edge.target === focus
          const label = relationshipLabelPosition(a, b)
          return <g key={[edge.source, edge.target].sort().join(':')} className={`graph-edge ${active ? '' : 'dim'}`}>
            <title>{edge.evidence}（{edge.origin === 'opening' ? '开局前情 · ' : ''}第 {edge.page} 页）</title>
            <path d={`M ${a.x} ${a.y} L ${b.x} ${b.y}`} />
            <text x={label.x} y={label.y} dominantBaseline="middle">{edge.label}</text>
          </g>
        })}
        {layout.nodes.map((person) => {
          const p = point(person)
          return <g key={person.id} transform={`translate(${p.x} ${p.y})`}
            data-character-status={person.status?.code ?? 'unknown'}
            className={`graph-node ${person.is_player ? 'player' : ''} ${focus && focus !== person.id && !adjacent.has(person.id) ? 'dim' : ''}`}
            role="button" tabIndex={0} aria-label={`查看${person.name}的人物卡`}
            aria-description={person.status?.label ?? '状态未确认'}
            onFocus={() => setFocus(person.id)} onBlur={() => setFocus(null)}
            onMouseEnter={() => setFocus(person.id)} onMouseLeave={() => setFocus(null)}
            onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(person) } }}
            onPointerDown={(event) => { event.stopPropagation(); drag.current = { id: person.id, x: event.clientX, y: event.clientY, moved: false }; event.currentTarget.setPointerCapture(event.pointerId) }}
            onPointerUp={() => { const moved = drag.current?.moved; drag.current = null; if (!moved) onSelect(person) }}>
            <title>{person.name}：{person.status?.label ?? '状态未确认'}</title>
            <circle className="graph-node-halo" r={person.is_player ? 40 : 36} />
            <GraphPortrait portrait={person.portrait} radius={person.is_player ? 34 : 30} />
            <circle className="graph-node-frame" r={person.is_player ? 34 : 30} />
            <text className="graph-name" y="54">{person.name}{person.is_player ? ' · 你' : ''}</text>
            {person.status && person.status.code !== 'unknown' && <text className="graph-status" y="70">{person.status.label}</text>}
          </g>
        })}
      </g>
    </svg>
    {!layout.edges.length && <p className="graph-caption">暂无已确认关系</p>}
  </div>
}
