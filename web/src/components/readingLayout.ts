export interface ReadingSection {
  paragraphs: string[]
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
