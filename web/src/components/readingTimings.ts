export type ReadingTiming = {
  attempt_id: string
  request_id: string
  session_id: string
  kind: 'opening' | 'turn'
  branch_id: string | null
  started_at: string
  first_text_ms: number | null
  full_text_ms: number | null
  directions_available_ms: number | null
  outcome: 'pending' | 'completed' | 'failed' | 'interrupted'
}

// Measure rendered milestones against this attempt, including cache hits and
// retries. Stream reset does not erase the first text the reader already saw.
export function createReadingTiming(
  identity: Pick<ReadingTiming, 'attempt_id' | 'request_id' | 'session_id'> & { kind?: ReadingTiming['kind'] },
  publish: (record: ReadingTiming) => void,
  clock: () => number = () => performance.now(),
) {
  const start = clock()
  const record: ReadingTiming = {
    kind: 'turn', ...identity, branch_id: null, started_at: new Date().toISOString(),
    first_text_ms: null, full_text_ms: null, directions_available_ms: null,
    outcome: 'pending',
  }
  const emit = () => publish({ ...record })
  emit()
  return {
    confirm(branchId: string, sessionId?: string) {
      record.branch_id = branchId
      if (sessionId) record.session_id = sessionId
    },
    rendered(text: string, streaming: boolean, branchId: string | undefined, directionsReady: boolean) {
      if (record.outcome === 'failed' || record.outcome === 'interrupted') return
      // A restored previous page or a different historical branch is not this result.
      if (!streaming && branchId !== record.branch_id) return
      const ms = Math.max(0, Math.round(clock() - start))
      let changed = false
      if (text.trim() && record.first_text_ms === null) {
        record.first_text_ms = ms
        changed = true
      }
      if (!streaming && text.trim() && record.full_text_ms === null) {
        record.full_text_ms = ms
        record.outcome = 'completed'
        changed = true
      }
      if (!streaming && record.full_text_ms !== null && directionsReady && record.directions_available_ms === null) {
        record.directions_available_ms = ms
        changed = true
      }
      if (changed) emit()
    },
    finish(outcome: 'failed' | 'interrupted') {
      if (record.outcome !== 'pending') return
      record.outcome = outcome
      emit()
    },
  }
}

export const READING_TIMINGS_KEY = 'story-reading-timings:v1'

// Diagnostics stay in this tab, bounded to 50 attempts, without player input
// or prose. Storage failures must never interrupt reading.
export function saveReadingTiming(record: ReadingTiming, storage: Pick<Storage, 'getItem' | 'setItem'>) {
  try {
    const raw: unknown = JSON.parse(storage.getItem(READING_TIMINGS_KEY) ?? '[]')
    const previous = Array.isArray(raw) ? raw : []
    const records = previous.filter((r) => r && r.attempt_id !== record.attempt_id)
    storage.setItem(READING_TIMINGS_KEY, JSON.stringify([...records, record].slice(-50)))
  } catch { /* Metrics are optional. */ }
}
