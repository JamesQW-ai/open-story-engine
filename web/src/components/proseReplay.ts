// Reveal only saved prose. Aborting stops display work, never changes the text.
export function replayProse(text: string, publish: (text: string) => void, signal: AbortSignal,
  clock = {
    setTimeout: (callback: () => void, delay: number) => globalThis.setTimeout(callback, delay),
    clearTimeout: (timer: ReturnType<typeof setTimeout>) => globalThis.clearTimeout(timer),
  }): Promise<void> {
  const characters = Array.from(text)
  return new Promise(resolve => {
    let offset = 0
    let timer: ReturnType<typeof setTimeout>
    const finish = () => {
      clock.clearTimeout(timer)
      signal.removeEventListener('abort', finish)
      resolve()
    }
    const tick = () => {
      if (signal.aborted) { finish(); return }
      offset = Math.min(characters.length, offset + 8)
      publish(characters.slice(0, offset).join(''))
      if (offset === characters.length) finish()
      else timer = clock.setTimeout(tick, 24)
    }
    if (signal.aborted) { resolve(); return }
    signal.addEventListener('abort', finish, { once: true })
    tick()
  })
}
