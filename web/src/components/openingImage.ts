import type { PublishedScene } from '../api/types'

// Decide once before prose starts. A late load must never insert a new top image.
export function prepareOpeningImage(asset: PublishedScene | null | undefined, signal: AbortSignal,
  timeoutMs = 1500, createImage = () => new Image()): Promise<PublishedScene | null> {
  if (!asset || signal.aborted) return Promise.resolve(null)
  return new Promise((resolve) => {
    const image = createImage()
    let settled = false
    const finish = (value: PublishedScene | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', cancel)
      image.onload = image.onerror = null
      resolve(value)
    }
    const cancel = () => finish(null)
    const timer = setTimeout(cancel, timeoutMs)
    signal.addEventListener('abort', cancel, { once: true })
    image.onload = () => finish(asset)
    image.onerror = cancel
    image.src = asset.url
    if (image.complete) finish(image.naturalWidth > 0 ? asset : null)
  })
}
