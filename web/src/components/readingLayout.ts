export interface ReadingSection {
  paragraphs: string[]
}

export const LONG_SCENE_CJK = 1000

export function cjkCount(text: string): number {
  return (text.match(/[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u{20000}-\u{2ebef}]/gu) ?? []).length
}

// Use the complete saved body so the slot cannot move as visible prose grows.
export function illustrationAnchor(text: string, selected = false): number | null {
  if (!selected && cjkCount(text) < LONG_SCENE_CJK) return null
  const paragraphs = readingSections(text).flatMap(section => section.paragraphs)
  const middle = paragraphs.reduce((size, paragraph) => size + Array.from(paragraph).length, 0) / 2
  let size = 0, best = 0, distance = Infinity
  for (let index = 0; index < paragraphs.length - 1; index++) {
    size += Array.from(paragraphs[index]).length
    if (Math.abs(size - middle) < distance) { best = index; distance = Math.abs(size - middle) }
  }
  return best
}

// Greedy boundaries stay in place as streamed text grows. Never rebalance
// earlier paragraphs or discard prose to fit a visual block.
export function readingSections(text: string): ReadingSection[] {
  const paragraphs: string[] = []
  for (const paragraph of text.split(/\n+/).filter((p) => p.trim())) {
    let part = ''
    let size = 0
    for (const char of paragraph) {
      part += char
      size++
      if ((size >= 500 && /[。！？；.!?]/.test(char)) || size >= 900) {
        paragraphs.push(part)
        part = ''
        size = 0
      }
    }
    if (part) paragraphs.push(part)
  }
  const sections: ReadingSection[] = []
  let current: string[] = []
  let size = 0
  for (const paragraph of paragraphs) {
    current.push(paragraph)
    size += [...paragraph].length
    if (size >= 600) {
      sections.push({ paragraphs: current })
      current = []
      size = 0
    }
  }
  if (current.length) sections.push({ paragraphs: current })
  return sections
}
