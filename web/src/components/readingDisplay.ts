// A visible saved paragraph is evidence of display, not proof of reading.
// Streaming drafts and loading states are excluded by the caller.
export function observeReadingDisplay(
  root: HTMLElement,
  report: () => Promise<unknown>,
  env = { document, window, IntersectionObserver },
) {
  let stopped = false
  let sent = false
  let pending = false
  let attempts = 0
  let frame = 0
  let retry = 0
  const visible = new Set<Element>()
  const eligible = () => !stopped && env.document.visibilityState === 'visible' && root.isConnected &&
    [...visible].some(element => {
      const box = element.getBoundingClientRect()
      return element.isConnected && box.width > 0 && box.height > 0 && box.bottom > 0 &&
        box.right > 0 && box.top < env.window.innerHeight && box.left < env.window.innerWidth
    })
  const schedule = () => {
    if (frame || sent || pending || attempts >= 2 || !eligible()) return
    // Allow a paint before acknowledging the paragraph, then recheck visibility.
    frame = env.window.requestAnimationFrame(() => {
      frame = env.window.requestAnimationFrame(() => {
        frame = 0
        if (!eligible()) return
        pending = true
        attempts++
        void Promise.resolve().then(report).then(() => { sent = true }).catch(() => {
          if (!stopped && attempts < 2) retry = env.window.setTimeout(schedule, 1000)
        }).finally(() => { pending = false })
      })
    })
  }
  const observer = new env.IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting) visible.add(entry.target)
      else visible.delete(entry.target)
    }
    schedule()
  })
  root.querySelectorAll('.passage').forEach(paragraph => observer.observe(paragraph))
  env.document.addEventListener('visibilitychange', schedule)
  return () => {
    stopped = true
    observer.disconnect()
    env.document.removeEventListener('visibilitychange', schedule)
    env.window.cancelAnimationFrame(frame)
    env.window.clearTimeout(retry)
  }
}
