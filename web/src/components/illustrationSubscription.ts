// Image leases only poll while work is pending. Draw is an explicit, once-only
// request per active lease; restoring a page never repeats that request.
export function subscribeIllustration<T extends { items: { status: string }[]; can_generate?: boolean }>(
  callbacks: {
    prepare: (subscriber: string, draw: boolean) => Promise<T>
    release: (subscriber: string) => Promise<unknown>
    subscription: (subscriber: string | null) => void
    ready: (result: T) => void
    error?: () => void
  },
  env = { window, newSubscriber: () => crypto.randomUUID() as string },
) {
  type Lease = { id: string; timer?: number; pending: boolean; wantDraw: boolean; drawing: boolean; failures: number }
  let active: Lease | null = null
  let disposed = false
  const release = (lease: Lease) => {
    void Promise.resolve().then(() => callbacks.release(lease.id)).catch(() => undefined)
  }
  const schedule = (lease: Lease, delay: number) => {
    if (active !== lease) return
    env.window.clearTimeout(lease.timer)
    lease.timer = env.window.setTimeout(() => void poll(lease), delay)
  }
  async function poll(lease: Lease) {
    if (active !== lease || lease.pending) return
    env.window.clearTimeout(lease.timer)
    lease.pending = true
    const draw = lease.wantDraw
    lease.wantDraw = false
    try {
      const result = await callbacks.prepare(lease.id, draw)
      if (active !== lease) return
      lease.failures = 0
      if (draw && result.can_generate) lease.drawing = false
      callbacks.ready(result)
      if (result.items.some(item => ['queued', 'generating'].includes(item.status))) schedule(lease, 1500)
    } catch {
      lease.drawing = false
      if (++lease.failures < 3) schedule(lease, 4000)
      else if (active === lease) callbacks.error?.()
    } finally {
      lease.pending = false
      if (active !== lease) release(lease)
      else if (lease.wantDraw) schedule(lease, 0)
    }
  }
  const stop = () => {
    const lease = active
    if (!lease) return
    active = null
    env.window.clearTimeout(lease.timer)
    callbacks.subscription(null)
    release(lease)
  }
  const start = () => {
    if (disposed || active) return
    const lease: Lease = { id: env.newSubscriber(), pending: false, wantDraw: false, drawing: false, failures: 0 }
    active = lease
    callbacks.subscription(lease.id)
    void poll(lease)
  }
  const resume = (event: PageTransitionEvent) => { if (event.persisted) start() }
  env.window.addEventListener('pagehide', stop)
  env.window.addEventListener('pageshow', resume)
  start()
  return {
    draw: () => {
      const lease = active
      if (!lease || lease.drawing) return
      lease.drawing = true
      lease.wantDraw = true
      void poll(lease)
    },
    cancel: stop,
    cleanup: () => {
      disposed = true
      stop()
      env.window.removeEventListener('pagehide', stop)
      env.window.removeEventListener('pageshow', resume)
    },
  }
}
