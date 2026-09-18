import type { BranchView, PackageCatalog } from '../api/types'

export function identityEntry(catalog: PackageCatalog, characterId: string) {
  const person = identityCharacters(catalog).find((c) => c.id === characterId)
  const declared = person?.defaultEntryPointId
  if (typeof declared === 'string')
    return catalog.entries.find((e) => e.id === declared && e.source_character_ids.includes(characterId))
  const available = catalog.entries.filter((e) =>
    e.source_character_ids.includes(characterId),
  )
  const name = person?.name
  // Prefer a declared event led by this character to another protagonist's opening.
  const led = name ? available.filter((e) => e.summary.startsWith(name)) : []
  led.sort(
    (a, b) =>
      (a.chapter_id ?? '').localeCompare(b.chapter_id ?? '', undefined, {
        numeric: true,
      }) || a.beat_id.localeCompare(b.beat_id, undefined, { numeric: true }),
  )
  return led[0] ?? available[0] ?? catalog.entries[0]
}

export function identityCharacters(catalog: PackageCatalog) {
  const seen = new Set<string>()
  return [...catalog.characters, ...(catalog.supporting_characters ?? [])].filter((person) => {
    if (seen.has(person.id)) return false
    seen.add(person.id)
    return true
  })
}

export function prologueText(catalog: PackageCatalog): string {
  if (catalog.package.title.includes('太虚遗录'))
    return '玄霄宗的山门即将合拢。一个重伤的陌生人倒在封山线外，手边抓着一盏没有点燃的灯。守门弟子按住剑柄，不许陆照临靠近。\n他为失踪的父亲而来。三年前，父亲押送的也是引路灯；此刻，石阶下刚亮起的青白光已经熄灭。\n门一旦合上，人和线索都会留在雨里。若你身在局中，会先救人、追查那盏灯，还是守住门规？'
  const text = catalog.package.summary
    .replace(/。；/g, '。')
    .replace(/\s+/g, '')
  return [...text].length <= 50 ? text : [...text].slice(0, 49).join('') + '…'
}

export function identityText(
  _catalog: PackageCatalog,
  person: PackageCatalog['characters'][number],
) {
  return (
    [person.menuDescription, person.description]
      .filter((s, i, all) => s && all.indexOf(s) === i)
      .join(' ') || '以这个身份走进故事，决定如何面对眼前的处境。'
  )
}

export interface StoryChoice {
  key: string
  title: string
  summary: string
  payload: { direction_id?: string; text?: string }
}
export function storyChoices(
  branch: BranchView,
  _place?: string | null,
): StoryChoice[] {
  if (
    branch.kind === 'source_entry' &&
    (branch.openingActions?.length ?? 0) > 0
  )
    return branch.openingActions!.map((action, i) => ({
      key: `opening-${i}`,
      ...action,
      payload: { text: `${action.title}。${action.summary}` },
    }))
  if (branch.readerChoices?.length)
    return branch.readerChoices.map((c) => ({ key: c.id, title: c.title, summary: c.summary, payload: { text: c.summary } }))
  const seen = new Set<string>()
  return branch.nextDirections
    .filter((d) => !['继续当前目标', '继续故事', '继续推进'].includes(d.title))
    .filter((d) => { if (seen.has(d.title)) return false; seen.add(d.title); return true })
    .map((d) => ({
      key: d.id,
      title:
        /^推进[：:]/.test(d.title) && d.summary
          ? `继续：${[...d.summary.replace(/[。；].*$/, '')].slice(0, 28).join('')}`
          : d.title,
      summary: d.summary || '沿着这一方向行动，看看眼前的局面如何变化。',
      payload: { direction_id: d.id } as StoryChoice['payload'],
    }))
}
