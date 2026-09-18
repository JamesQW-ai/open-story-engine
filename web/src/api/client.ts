import type {
  RouteClosure,
  EndingType,
  EndingProposal,
  EndingAttempt,
  PreparedChoice,
  SceneIllustrations,
  Journey,
  JournalPerson,
  BranchPage,
  BranchView,
  ContextRequest,
  ContextView,
  HealthResponse,
  PackageCatalog,
  PackageList,
  PlayContinueRequest,
  PlayContinueResponse,
  PlayCreateRequest,
  PlayCreateResponse,
  SessionList,
  SessionView,
  SourceChapterView,
  StateView,
} from './types'

const BASE = '/api/v1'

export class ApiError extends Error {
  status: number
  code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError(0, 'network_error', '暂时连接不上，请稍后重试。')
  }
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON 响应按 HTTP 状态处理
  }
  if (!response.ok) {
    const detail = (
      body as { error?: { code?: string; message?: string } } | null
    )?.error
    throw new ApiError(
      response.status,
      detail?.code ?? `http_${response.status}`,
      detail?.message ?? `请求失败（HTTP ${response.status}）`,
    )
  }
  return body as T
}

async function streamRequest<T>(
  path: string,
  payload: PlayContinueRequest | PlayCreateRequest,
  onDelta: (text: string) => void,
  onReset: () => void,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(payload),
    signal,
  })
  if (!response.ok || !response.body)
    throw new ApiError(
      response.status,
      'stream_unavailable',
      '暂时无法续写，请重试。',
    )
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const lines = frame.split('\n')
        const event = lines
          .find((line) => line.startsWith('event:'))
          ?.slice(6)
          .trim()
        const raw = lines
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n')
        if (!raw) continue
        const data = JSON.parse(raw)
        if (event === 'delta' && typeof data.text === 'string')
          onDelta(data.text)
        if (event === 'reset') onReset()
        if (event === 'error')
          throw new ApiError(
            data.status ?? 503,
            data.code ?? 'generation_failed',
            data.message ?? '续写未完成。',
          )
        if (event === 'done') return data as T
      }
      if (done)
        throw new ApiError(
          0,
          'stream_interrupted',
          '连接中断，请重试以恢复进度。',
        )
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

export const api = {
  viewIllustrations: (sid: string, bid: string, signal: AbortSignal) =>
    request<SceneIllustrations>(`/sessions/${sid}/branches/${bid}/illustrations`, { signal }),
  illustrations: (sid: string, bid: string, subscriber: string, draw = false) =>
    request<SceneIllustrations>(`/sessions/${sid}/branches/${bid}/illustrations?subscriber=${encodeURIComponent(subscriber)}&draw=${draw}`,
      { method: 'POST' }),
  releaseIllustrations: (sid: string, bid: string, subscriber: string) =>
    request(`/sessions/${sid}/branches/${bid}/illustrations/release?subscriber=${encodeURIComponent(subscriber)}`,
      { method: 'POST', keepalive: true }),
  shownIllustration: (sid: string, bid: string, subscriber: string, ms: number) =>
    request(`/sessions/${sid}/branches/${bid}/illustrations/shown?subscriber=${encodeURIComponent(subscriber)}&display_ms=${Math.min(3600000, Math.max(0, Math.round(ms)))}`,
      { method: 'POST', keepalive: true }),
  prepareChoices: (sessionId: string, parent: string, subscriber: string, history_id?: string) =>
    request<{ parent_branch_id: string; choices: PreparedChoice[] }>(
      `/sessions/${encodeURIComponent(sessionId)}/choices/prepare`,
      { method: 'POST', body: JSON.stringify({ parent_branch_id: parent, subscriber_id: subscriber, history_id }) },
    ),
  releaseChoices: (sessionId: string, parent: string, subscriber: string) =>
    request(`/sessions/${encodeURIComponent(sessionId)}/choices/release`, {
      method: 'POST', keepalive: true,
      body: JSON.stringify({ parent_branch_id: parent, subscriber_id: subscriber }),
    }),
  streamTurn: (
    sid: string,
    payload: PlayContinueRequest,
    delta: (text: string) => void,
    reset: () => void,
    signal?: AbortSignal,
  ) =>
    streamRequest<PlayContinueResponse>(
      `/sessions/${encodeURIComponent(sid)}/branches/stream`,
      payload,
      delta,
      reset,
      signal,
    ),
  streamOpening: (
    payload: PlayCreateRequest,
    delta: (text: string) => void,
    reset: () => void,
    signal?: AbortSignal,
  ) =>
    streamRequest<PlayCreateResponse>(
      '/sessions/stream',
      payload,
      delta,
      reset,
      signal,
    ),
  journey: (sid: string, bid: string) =>
    request<Journey>(
      `/sessions/${encodeURIComponent(sid)}/journey?branch_id=${encodeURIComponent(bid)}`,
    ),
  characterProfile: (sid: string, branch_id: string, character_id: string) =>
    request<JournalPerson>(`/sessions/${encodeURIComponent(sid)}/character-profile`, {
      method: 'POST',
      body: JSON.stringify({ branch_id, character_id }),
    }),
  renameSession: (sid: string, title: string) =>
    request<{ title: string }>(`/sessions/${encodeURIComponent(sid)}/rename`, {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),
  endRoute: (sid: string, branch_id: string) =>
    request<{ status: string }>(`/sessions/${encodeURIComponent(sid)}/end`, {
      method: 'POST',
      body: JSON.stringify({ branch_id }),
    }),
  routeClosure: (sid: string, bid: string) =>
    request<RouteClosure>(`/sessions/${encodeURIComponent(sid)}/route-closure?branch_id=${encodeURIComponent(bid)}`),
  planClosure: (sid: string, branch_id: string, intended_type: EndingType | null) =>
    request<RouteClosure>(`/sessions/${encodeURIComponent(sid)}/route-closure`, {
      method: 'POST', body: JSON.stringify({ branch_id, intended_type }),
    }),
  endingProposals: (sid: string, bid: string) =>
    request<EndingProposal[]>(`/sessions/${encodeURIComponent(sid)}/ending-proposals?branch_id=${encodeURIComponent(bid)}`),
  proposeEnding: (sid: string, branch_id: string, attempt: EndingAttempt) =>
    request<EndingProposal>(`/sessions/${encodeURIComponent(sid)}/ending-proposals`, {
      method: 'POST', body: JSON.stringify({ branch_id, ...attempt }),
    }),
  commitEnding: (sid: string, branch_id: string, proposal: string) =>
    request<{ status: string }>(`/sessions/${encodeURIComponent(sid)}/ending-proposals/${encodeURIComponent(proposal)}/commit`, {
      method: 'POST', body: JSON.stringify({ branch_id }),
    }),
  cancelEnding: (sid: string, branch_id: string, proposal: string) =>
    request<EndingProposal>(`/sessions/${encodeURIComponent(sid)}/ending-proposals/${encodeURIComponent(proposal)}/cancel`, {
      method: 'POST', body: JSON.stringify({ branch_id }),
    }),
  health: () => request<HealthResponse>('/health'),
  recordReadingDisplay: (sid: string, branch_id: string) =>
    request<{ recorded: boolean; deduplicated?: boolean; reason?: string }>(
      `/sessions/${encodeURIComponent(sid)}/reading-receipts`, {
        method: 'POST', body: JSON.stringify({ branch_id }),
      }),
  listPackages: () => request<PackageList>('/packages'),
  getPackage: (packageId: string, version: string) =>
    request<PackageCatalog>(
      `/packages/${encodeURIComponent(packageId)}/${encodeURIComponent(version)}`,
    ),
  listSessions: () => request<SessionList>('/sessions'),
  deleteSession: (sessionId: string) =>
    request<void>(`/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),
  getSession: (sessionId: string) =>
    request<SessionView>(
      `/sessions/${encodeURIComponent(sessionId)}?include_branches=false`,
    ),
  getBranches: (sessionId: string, afterSequence: number, limit = 50) =>
    request<BranchPage>(
      `/sessions/${encodeURIComponent(sessionId)}/branches?limit=${limit}&after_sequence=${afterSequence}&include_actions=true`,
    ),
  getBranch: (sessionId: string, branchId: string) =>
    request<BranchView>(
      `/sessions/${encodeURIComponent(sessionId)}/branches/${encodeURIComponent(branchId)}`,
    ),
  getState: (sessionId: string, branchId?: string) =>
    request<StateView>(
      `/sessions/${encodeURIComponent(sessionId)}/state${branchId ? `?branch_id=${encodeURIComponent(branchId)}` : ''}`,
    ),
  buildContext: (payload: ContextRequest) =>
    request<ContextView>('/context/build', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getSourceChapter: (sessionId: string, branchId: string) =>
    request<SourceChapterView>(
      `/sessions/${encodeURIComponent(sessionId)}/branches/${encodeURIComponent(branchId)}/source-chapter`,
    ),
  createSessionPlay: (payload: PlayCreateRequest) =>
    request<PlayCreateResponse>('/sessions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  continueTurn: (sessionId: string, payload: PlayContinueRequest) =>
    request<PlayContinueResponse>(
      `/sessions/${encodeURIComponent(sessionId)}/branches`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    ),
}
