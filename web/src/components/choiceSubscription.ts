// Each restored page owns a new lease. Late responses only release their own
// lease and can never restart polling or publish into the restored reader.
export function subscribeChoices<T extends { parent_branch_id: string }>(
  parent: string,
  callbacks: {
    prepare: (subscriber: string) => Promise<T>
    release: (subscriber: string) => Promise<unknown>
    subscription: (subscriber: string | null) => void
    locked: () => boolean
    ready: (result: T) => void
    error: () => void
  },
  env = { window, newSubscriber: () => crypto.randomUUID() as string },
) {
  type Lease = { subscriber: string; timer?: number }
  let active: Lease | null = null
  let disposed = false
  const release = (lease: Lease) => {
    void Promise.resolve().then(() => callbacks.release(lease.subscriber)).catch(() => undefined)
  }
  const schedule = (lease: Lease, delay = 2500) => {
    if (active === lease) lease.timer = env.window.setTimeout(() => void refresh(lease), delay)
  }
  async function refresh(lease: Lease) {
    if (active !== lease) return
    lease.timer = undefined
    if (callbacks.locked()) { schedule(lease); return }
    try {
      const result = await callbacks.prepare(lease.subscriber)
      if (active !== lease || callbacks.locked()) return
      if (result.parent_branch_id !== parent) throw new Error('choice_parent_mismatch')
      callbacks.ready(result)
      const choices = (result as T & { choices?: { status?: string }[] }).choices ?? []
      const preparing = choices.some(choice => choice.status === 'queued' || choice.status === 'generating' || choice.status === 'validating')
      schedule(lease, preparing ? 1000 : 2500)
    } catch {
      if (active === lease && !callbacks.locked()) callbacks.error()
    } finally {
      // The server may finish registering this lease after pagehide released
      // it. Keep the response handler so that completion releases it again.
      if (active !== lease) release(lease)
      else if (lease.timer === undefined) schedule(lease)
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
    const lease: Lease = { subscriber: env.newSubscriber() }
    active = lease
    callbacks.subscription(lease.subscriber)
    void refresh(lease)
  }
  const resume = (event: PageTransitionEvent) => { if (event.persisted) start() }
  env.window.addEventListener('pagehide', stop)
  env.window.addEventListener('pageshow', resume)
  start()
  return () => {
    disposed = true
    stop()
    env.window.removeEventListener('pagehide', stop)
    env.window.removeEventListener('pageshow', resume)
  }
}
