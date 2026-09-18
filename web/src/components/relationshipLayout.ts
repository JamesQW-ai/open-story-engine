import type { JournalPerson, JournalRelationship } from '../api/types.ts'

export function relationshipLayout(people: JournalPerson[], relationships: JournalRelationship[]) {
  const ordered = [...people].sort((a, b) => Number(b.is_player) - Number(a.is_player) || a.first_page - b.first_page || a.id.localeCompare(b.id))
  const center = { x: 210, y: 205 }
  const remaining = ordered.length - 1
  const ringCount = remaining > 0 ? Math.ceil(remaining / 8) : 0
  const ringSizes = Array.from({ length: ringCount }, (_, ring) => {
    const base = Math.floor(remaining / ringCount)
    return base + (ring < remaining % ringCount ? 1 : 0)
  })
  const ringOffsets = ringSizes.reduce<number[]>((offsets, size, ring) => {
    offsets.push((offsets[ring - 1] ?? 0) + size)
    return offsets
  }, [])
  const nodes = ordered.map((person, i) => {
    if (i === 0) return { ...person, ...center }
    const ordinal = i - 1
    const ring = ringOffsets.findIndex((offset) => ordinal < offset)
    const previous = ring > 0 ? ringOffsets[ring - 1] : 0
    const slot = ordinal - previous
    const size = ringSizes[ring]
    const radius = ringCount === 1 ? 145 : 95 + (50 * ring) / (ringCount - 1)
    const angle = -Math.PI / 2 + slot * 2 * Math.PI / size + (ring % 2 ? Math.PI / size : 0)
    return { ...person, x: center.x + Math.cos(angle) * radius, y: center.y + Math.sin(angle) * radius }
  })
  const ids = new Set(nodes.map((p) => p.id))
  const edges = relationships.filter((r) => r.source !== r.target && ids.has(r.source) && ids.has(r.target))
  return { nodes, edges }
}
