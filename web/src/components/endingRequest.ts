import type { EndingAttempt } from '../api/types.ts'

// Retain the request identity before POST so a lost response or page reload
// can recover the same review instead of spending on another one.
export function endingRequest(key: string, storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
  newId: () => string = () => crypto.randomUUID()) {
  let current: EndingAttempt | null = null
  try {
    const saved = JSON.parse(storage.getItem(key) ?? 'null')
    if (saved && ['request_id', 'outcome_summary', 'ending_quote'].every(k => typeof saved[k] === 'string')) current = saved
  } catch { /* A damaged local draft cannot authorize a review. */ }
  return {
    saved: () => current,
    prepare(summary: string, quote: string) {
      if (!current || current.outcome_summary !== summary || current.ending_quote !== quote) {
        current = { request_id: newId(), outcome_summary: summary, ending_quote: quote }
        // If storage is unavailable, retain in memory but do not send a request
        // that could lose its identity on reload.
        storage.setItem(key, JSON.stringify(current))
      } else storage.setItem(key, JSON.stringify(current))
      return current
    },
    clear() { storage.removeItem(key); current = null },
  }
}

// Serialize button actions and suppress responses after the panel is removed.
export function closureActions(callbacks: { busy: (value: boolean) => void; error: (error: unknown) => void }) {
  let active = true, running = false
  return {
    async run<T>(work: () => Promise<T>, accept: (value: T) => void) {
      if (!active || running) return
      running = true
      callbacks.busy(true)
      try {
        const result = await work()
        if (active) accept(result)
      } catch (error) {
        if (active) callbacks.error(error)
      } finally {
        running = false
        if (active) callbacks.busy(false)
      }
    },
    dispose() { active = false },
  }
}
