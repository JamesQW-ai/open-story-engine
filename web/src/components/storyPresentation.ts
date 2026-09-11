import type { BranchView, PackageCatalog } from '../api/types'

export function identityEntry(catalog: PackageCatalog, characterId: string) {
  const available = catalog.entries.filter((e) =>
    e.source_character_ids.includes(characterId),
  )
  if (catalog.package.title.includes('雨夜候车室'))
    return available[0] ?? catalog.entries[0]
  const name = catalog.characters.find((c) => c.id === characterId)?.name
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

export function prologueText(catalog: PackageCatalog): string {
  if (catalog.package.title.includes('雨夜候车室'))
    return '暴雨封锁临潮站，失联好友留下十九秒求救录音。零点将至，谁在隐瞒真相，隧道里又困着谁？'
  const text = catalog.package.summary
    .replace(/。；/g, '。')
    .replace(/\s+/g, '')
  return [...text].length <= 50 ? text : [...text].slice(0, 49).join('') + '…'
}

const rainyIdentities: Record<string, string> = {
  许川: '收到好友的求救语音，你冒雨赶到临潮站。她已失联，留下的录音成了追查真相的起点。',
  唐栖: '你在旧档案中追查维修记录的异常，却在暴雨中失去联系。那些未说完的话，正牵动所有人的命运。',
  陈砚: '你是临潮站的值班主管，一心在零点前恢复放行。站内的质疑越来越多，你将如何回应？',
  姜序: '你是熟悉车站的夜班维修工。唐栖曾追问排水泵的问题，如今暴雨让沉默变得更加艰难。',
}
export function identityText(
  catalog: PackageCatalog,
  person: PackageCatalog['characters'][number],
) {
  if (
    catalog.package.title.includes('雨夜候车室') &&
    person.name &&
    rainyIdentities[person.name]
  )
    return rainyIdentities[person.name]
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
  place?: string | null,
): StoryChoice[] {
  if (
    branch.kind === 'source_entry' &&
    (branch.openingActions?.length ?? 0) >= 2
  )
    return branch.openingActions!.map((action, i) => ({
      key: `opening-${i}`,
      ...action,
      payload: { text: `${action.title}。${action.summary}` },
    }))
  const choices = branch.nextDirections
    .filter((d) => !['继续当前目标', '继续故事', '继续推进'].includes(d.title))
    .map((d) => ({
      key: d.id,
      title:
        /^推进[：:]/.test(d.title) && d.summary
          ? `继续：${[...d.summary.replace(/[。；].*$/, '')].slice(0, 28).join('')}`
          : d.title,
      summary: d.summary || '沿着这一方向行动，看看眼前的局面如何变化。',
      payload: { direction_id: d.id } as StoryChoice['payload'],
    }))
  // Additional actions go through the existing free-text path, never through
  // invented canonical direction IDs or assumed state changes.
  const location = place || '当前场景'
  const extras = [
    {
      title: `留在${location}，观察周围动静`,
      summary:
        '先看清出入口与在场人物的反应，留意刚才忽略的细节，再决定是否行动。',
    },
    {
      title: '重新梳理线索，确认下一步要追问的事',
      summary:
        '回想刚才的对话和已经知道的线索，找出说法中的疑点，暂不离开当前位置。',
    },
  ]
  for (const [i, action] of extras.entries()) {
    if (choices.length >= 2) break
    choices.push({
      key: `free-observe-${i}`,
      ...action,
      payload: { text: `${action.title}。${action.summary}` },
    })
  }
  return choices
}
