import type { JournalPerson, JournalRelationship } from '../api/types.ts'

export function relationshipLayout(people: JournalPerson[], relationships: JournalRelationship[]) {
  const ordered = [...people].sort((a, b) => Number(b.is_player) - Number(a.is_player) || a.first_page - b.first_page || a.id.localeCompare(b.id))
  const nodes = ordered.map((person, i) => {
    if (i === 0) return { ...person, x: 210, y: 205 }
    const angle = -Math.PI / 2 + (i - 1) * 2 * Math.PI / Math.max(1, ordered.length - 1)
    const radius = ordered.length > 8 && i % 2 === 0 ? 95 : 145
    return { ...person, x: 210 + Math.cos(angle) * radius, y: 205 + Math.sin(angle) * radius }
  })
  const ids = new Set(nodes.map((p) => p.id))
  const edges = relationships.filter((r) => r.source !== r.target && ids.has(r.source) && ids.has(r.target))
  return { nodes, edges }
}
