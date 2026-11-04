/** Typed client for the PAW API. All server errors share one envelope (see paw-server core/errors). */

export interface ErrorEnvelope {
  code: string
  message: string
  correlation_id: string
  retryable: boolean
  details?: Record<string, unknown>[]
}

export class ApiError extends Error {
  readonly status: number
  readonly envelope?: ErrorEnvelope

  constructor(status: number, message: string, envelope?: ErrorEnvelope) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.envelope = envelope
  }
}

export const API_BASE_URL: string = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/+$/, '')

// The owner token lives in sessionStorage only (cleared when the tab closes).
const TOKEN_KEY = 'paw.token'
export const getToken = (): string | null => {
  try {
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}
export const setToken = (t: string | null) => {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* storage blocked: session just won't persist */
  }
}
export const UNAUTHORIZED_EVENT = 'paw:unauthorized'

// Google sign-in uses an HttpOnly session cookie. The CSRF token (from /v1/auth/me) is kept in memory only and
// sent on every write; it is useless to another site because it is never in a cookie.
let csrfToken: string | null = null
export const setCsrf = (t: string | null) => {
  csrfToken = t
}
const isWrite = (method?: string) => !!method && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())

function isEnvelope(x: unknown): x is ErrorEnvelope {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as ErrorEnvelope).code === 'string' &&
    typeof (x as ErrorEnvelope).message === 'string'
  )
}

function toError(status: number, body: unknown): ApiError {
  const env = isEnvelope(body) ? body : undefined
  return new ApiError(status, env?.message ?? `Request failed (${status}).`, env)
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  opts: { base?: string; auth?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  const token = getToken()
  if (opts.auth !== false && token) headers.set('Authorization', `Bearer ${token}`)
  if (isWrite(init.method) && csrfToken && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrfToken)
  let res: Response
  try {
    res = await fetch(`${opts.base ?? API_BASE_URL}${path}`, { ...init, headers, credentials: 'include' })
  } catch {
    throw new ApiError(0, 'Unable to reach the PAW service.')
  }
  const body: unknown = await res.json().catch(() => undefined)
  if (!res.ok) {
    if (res.status === 401 && opts.auth !== false) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    throw toError(res.status, body)
  }
  return body as T
}

// ---- authentication (Google sign-in; see paw-server api/auth.py) ----
export interface AuthConfig {
  mode: 'token' | 'google' | 'both'
  google: boolean
}
export const getAuthConfig = () => request<AuthConfig>('/v1/auth/config', {}, { auth: false })

export type Me =
  | { mode: 'token' }
  | { mode: 'session'; email: string; csrf_token: string; expires_at: string; authenticated_at: string; stepup_valid: boolean }
/** auth:false = no bearer header and no global sign-out event on 401: this is a probe, a 401 just means "not signed in". */
export const getMeQuiet = () => request<Me>('/v1/auth/me', {}, { auth: false })
export const logout = (csrf: string) =>
  request<void>('/v1/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } }, { auth: false })

/** Full-page navigations (Google needs a real redirect, not fetch). return_to must be a same-site path. */
export const googleSignInUrl = (returnTo = '/') =>
  `${API_BASE_URL}/v1/auth/google/start?return_to=${encodeURIComponent(returnTo)}`
export const stepUpUrl = (returnTo = '/') => `${googleSignInUrl(returnTo)}&purpose=stepup`

// ---- integrations (see paw-server api/integrations.py) ----
export type IntegrationState = 'connected' | 'available' | 'needs_reauth' | 'unavailable'
export interface Integration {
  id: string
  name: string
  description: string
  kind: 'built_in' | 'connector'
  state: IntegrationState
  reason?: string
  account_email?: string
  connected_at?: string
  tools: { name: string; access: 'read' | 'write' }[]
}
export interface CalendarEvent {
  id: string | null
  title: string
  start: string | null
  end: string | null
  all_day: boolean
  location: string | null
}
export const listIntegrations = () => request<{ integrations: Integration[] }>('/v1/integrations')
export const calendarEvents = (days = 7) =>
  request<{ account_email: string; events: CalendarEvent[] }>(`/v1/integrations/google-calendar/events?days=${days}&limit=10`)
export const disconnectCalendar = () => request<void>('/v1/integrations/google-calendar', { method: 'DELETE' })
/** Full-page navigation: Google needs a real redirect. */
export const connectCalendarUrl = (returnTo = '/#/integrations') =>
  `${API_BASE_URL}/v1/integrations/google-calendar/connect?return_to=${encodeURIComponent(returnTo)}`

// ---- Admin Hermes agent (see paw-server api/agent.py) ----
export type RunState = 'QUEUED' | 'DISPATCHING' | 'RUNNING' | 'CANCELLING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'INTERRUPTED'
export const RUN_TERMINAL: RunState[] = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED']
export interface AgentRun {
  id: string
  session_id: string
  client_request_id: string
  message: string
  state: RunState
  output: string | null
  error_code: string | null
  usage: { input_tokens: number; output_tokens: number } | null
  queued_at: string
  started_at: string | null
  finished_at: string | null
}
export interface AgentEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
}
export interface RuntimeHealth {
  configured: boolean
  ready: boolean
  missing: string[]
}
export interface AgentSession {
  id: string; title: string; created_at: string; last_activity_at: string
  agent_actor_id: string; requester_actor_id: string; actor_name?: string
  kind: 'chat' | 'delegation'; forked_from_session_id: string | null; fork_seq: number | null
  delegated_from_session_id: string | null; root_session_id: string
}
export interface LedgerMessage {
  id: string; session_id: string; seq: number; kind: 'user' | 'assistant' | 'tool_call' | 'tool_result' | 'platform_event' | 'summary' | 'delegation'
  author_actor_id: string; run_id: string | null; tool_invocation_id: string | null
  status: 'complete' | 'unfinished'; created_at: string; body: string | null
  data: Record<string, unknown> | null; forgotten: boolean; source_message_id: string | null
}
export interface LedgerPage {
  messages: LedgerMessage[]; next_after: number | null
  summaries: { id: string; from_seq: number; to_seq: number; message_id: string; invalidated_at: string | null; model: string }[]
  children: AgentSession[]; summary_job: { state: string; error_code: string | null } | null
}
const jsonPost = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
export async function listAgentSessions(): Promise<{ sessions: AgentSession[] }> {
  const sessions: AgentSession[] = []
  let cursor: string | null = null
  for (;;) {
    const page: { sessions: AgentSession[]; next_cursor?: string | null } = await request(`/v1/agent-sessions${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)
    sessions.push(...page.sessions)
    if (!page.next_cursor) return { sessions }
    if (cursor === page.next_cursor) throw new ApiError(0, 'The conversation list could not be loaded.')
    cursor = page.next_cursor
  }
}
export const listAgentMessages = (id: string, after = 0) => request<LedgerPage>(`/v1/agent-sessions/${id}/messages?after=${after}&limit=200`)
export async function allAgentMessages(id: string): Promise<LedgerPage> {
  const messages: LedgerMessage[] = []
  let after = 0
  for (;;) {
    const page = await listAgentMessages(id, after)
    messages.push(...page.messages)
    if (page.next_after === null) return { ...page, messages }
    if (page.next_after <= after) throw new ApiError(0, 'The conversation could not be loaded.')
    after = page.next_after
  }
}
export const renameAgentSession = (id: string, title: string) => request<AgentSession>(`/v1/agent-sessions/${id}/rename`, jsonPost({ title }))
export const forkAgentSession = (id: string, messageId: string, message: string, clientRequestId: string) =>
  request<{ session: AgentSession; run: AgentRun }>(`/v1/agent-sessions/${id}/fork`, jsonPost({ message_id: messageId, message, client_request_id: clientRequestId }))
export const forgetAgentMessage = (id: string, messageId: string) => request(`/v1/agent-sessions/${id}/messages/${messageId}/forget`, { method: 'POST' })
export const deleteAgentSession = (id: string, includeDelegated: boolean) => request(`/v1/agent-sessions/${id}/delete`, jsonPost({ include_delegated: includeDelegated }))
export const listAgentActivity = (cursor?: string) => request<{ messages: LedgerMessage[]; next_cursor: string | null }>(`/v1/agent-activity${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)

export const getRuntimeHealth = () => request<RuntimeHealth>('/v1/runtime/health')
export const createAgentSession = (title = '') =>
  request<{ id: string }>('/v1/agent-sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
export const listAgentRuns = (sessionId: string) =>
  request<{ runs: AgentRun[] }>(`/v1/agent-sessions/${sessionId}/runs`)
export const submitAgentRun = (sessionId: string, clientRequestId: string, message: string) =>
  request<AgentRun>(`/v1/agent-sessions/${sessionId}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_request_id: clientRequestId, message }),
  })
export const getAgentRun = (runId: string) => request<AgentRun>(`/v1/agent-runs/${runId}`)
export const cancelAgentRun = (runId: string) => request<AgentRun>(`/v1/agent-runs/${runId}/cancel`, { method: 'POST' })

/**
 * Reads a run's stored events (server-sent events over fetch, so the Authorization header works).
 * Resolves with the last sequence number delivered; the caller reconnects with that number if the run is not finished.
 */
export async function streamRunEvents(
  runId: string,
  after: number,
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<number> {
  const token = getToken()
  let res: Response
  try {
    res = await fetch(`${API_BASE_URL}/v1/agent-runs/${runId}/events?after=${after}`, {
      signal,
      credentials: 'include',
      headers: { Accept: 'text/event-stream', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') return after
    throw new ApiError(0, 'Unable to reach the PAW service.')
  }
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    throw toError(res.status, await res.json().catch(() => undefined))
  }
  if (!res.body) throw new ApiError(0, 'The response could not be streamed.')
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let last = after
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let i: number
      while ((i = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, i)
        buf = buf.slice(i + 2)
        const data = /^data: (.+)$/m.exec(frame)?.[1]
        if (!data) continue // keepalive comment
        const ev = JSON.parse(data) as AgentEvent
        if (ev.seq <= last) continue // never show an event twice
        last = ev.seq
        onEvent(ev)
      }
    }
  } catch (e) {
    if ((e as Error).name === 'AbortError') return last
    throw new ApiError(0, 'The event stream was interrupted.')
  }
  return last
}

// ---- Admin Hermes identity (see paw-server api/profile.py) ----
export interface ProfileSpec {
  persona: {
    title: string
    summary: string
    expertise: string[]
    communication: { style: 'concise' | 'balanced' | 'detailed'; uncertainty: 'explicit' | 'implicit' }
  }
  ethos: string[]
  runtime: { harness: 'hermes'; model: string; max_iterations: number; max_run_seconds: number }
  tools: { grants: string[] }
  budgets: { per_run_usd: number; daily_usd: number }
}
export interface ProfileVersion {
  id: string
  version: number
  status: 'draft' | 'active' | 'retired'
  spec: ProfileSpec
  compiled_hash: string | null
  created_by: string
  created_at: string
  activated_at: string | null
}
export interface ProfileView {
  runtime_config: 'matches' | 'mismatch' | 'unknown'
  restart_required: boolean
  actor_id: string
  active: ProfileVersion | null
  versions: ProfileVersion[]
  known_tools: string[]
}
export interface DraftResult {
  id: string
  version: number
  status: string
  changes: string[]
  requires_confirmation: boolean
}
export interface ActivateResult {
  version: number
  compiled_hash: string
  changes: string[]
  restart_required: boolean
}
const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})
export const getProfile = () => request<ProfileView>('/v1/agent/profile')
export const createProfileDraft = (spec: ProfileSpec) => request<DraftResult>('/v1/agent/profile/versions', json({ spec }))
export const activateProfileVersion = (id: string, confirm: boolean) =>
  request<ActivateResult>(`/v1/agent/profile/versions/${id}/activate`, json({ confirm }))

// ---- readiness (public) ----
export interface Readiness {
  status: 'ready'
}
export const getReady = (base?: string) => request<Readiness>('/ready', {}, { base, auth: false })

// ---- documents ----
export type DocStatus =
  | 'RECEIVED' | 'STORED' | 'EXTRACTING' | 'CHUNKING' | 'EMBEDDING'
  | 'INDEXING' | 'COMPLETED' | 'FAILED' | 'UNSUPPORTED'
export const TERMINAL: DocStatus[] = ['COMPLETED', 'FAILED', 'UNSUPPORTED']

export interface VersionSummary {
  id: string
  version_no: number
  status: DocStatus
  error_code: string | null
  error_message: string | null
  status_updated_at: string
}
export interface DocumentSummary {
  id: string
  display_name: string
  media_type: string
  created_at: string
  current_version_id: string | null
  latest_version: VersionSummary
}
export interface DocumentList {
  items: DocumentSummary[]
  next_cursor: string | null
}
export interface EventOut {
  status: string
  detail: string | null
  created_at: string
}
export interface VersionDetail extends VersionSummary {
  sha256: string
  size_bytes: number
  parser_version: string | null
  embedding_model: string | null
  stage: string | null
  attempt_count: number | null
  dead_letter: boolean | null
  chunk_count: number
  events: EventOut[]
}
export interface DocumentDetail {
  id: string
  display_name: string
  media_type: string
  created_at: string
  current_version_id: string | null
  versions: VersionDetail[]
}
export interface UploadResponse {
  id: string
  version_id: string
  status_url: string
  duplicate: boolean
}

export const listDocuments = (cursor?: string) =>
  request<DocumentList>(`/v1/documents?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`)
export const getDocument = (id: string) => request<DocumentDetail>(`/v1/documents/${id}`)

/** XHR (not fetch) so the browser reports upload progress. */
export function uploadDocument(file: File, onProgress: (pct: number) => void): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE_URL}/v1/documents`)
    xhr.withCredentials = true
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken)
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100))
    xhr.onerror = () => reject(new ApiError(0, 'Unable to reach the PAW service.'))
    xhr.onload = () => {
      let body: unknown
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        body = undefined
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as UploadResponse)
      else {
        if (xhr.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
        reject(toError(xhr.status, body))
      }
    }
    const form = new FormData()
    form.append('file', file)
    xhr.send(form)
  })
}

// ---- retrieval ----
export interface SearchResult {
  document_id: string
  document_version_id: string
  chunk_id: string
  document_name: string
  media_type: string
  excerpt: string
  excerpt_start?: number
  excerpt_truncated: boolean
  retrieval_method: 'hybrid' | 'keyword' | 'vector'
  source: {
    page_number: number | null
    section: string | null
    source_start: number | null
    source_end: number | null
    source_unit: 'chars' | 'rows'
    ordinal: number
    url: string
  }
  scores: {
    fused: number
    keyword_rank: number | null
    vector_rank: number | null
  }
}
export interface SearchResponse {
  results: SearchResult[]
  meta: {
    fusion_version: string
    methods_used: string[]
    semantic?: boolean
    embedding_model?: string
    degraded: string | null
    latency_ms: number
    score_note: string
  }
}
export const search = (query: string, topK: number, documentIds?: string[]) =>
  request<SearchResponse>('/v1/retrieval/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK, document_ids: documentIds?.length ? documentIds : undefined }),
  })

export interface ChunkDetail {
  chunk_id: string
  document_id: string
  document_version_id: string
  document_name: string
  media_type: string
  ordinal: number
  text: string
  page_number: number | null
  section: string | null
  source_start: number | null
  source_end: number | null
  metadata: Record<string, unknown>
  is_current_version: boolean
  original_url: string
  previous_chunk_id: string | null
  next_chunk_id: string | null
}
export interface ChunkOut {
  id: string
  ordinal: number
  text: string
  page_number: number | null
  section: string | null
  source_start: number | null
  source_end: number | null
  metadata: Record<string, unknown>
}
export const listChunks = (docId: string, versionId: string, limit = 20) =>
  request<{ items: ChunkOut[]; next_cursor: number | null }>(
    `/v1/documents/${docId}/chunks?version_id=${versionId}&limit=${limit}`,
  )
export const getChunk = (id: string) => request<ChunkDetail>(`/v1/chunks/${id}`)

/** Authenticated download (the endpoint needs the bearer header, so a plain link would fail). */
export async function downloadOriginal(originalUrl: string, filename: string): Promise<void> {
  const token = getToken()
  let res: Response
  try {
    res = await fetch(`${API_BASE_URL}${originalUrl}`, {
      credentials: 'include',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
  } catch {
    throw new ApiError(0, 'Unable to reach the PAW service.')
  }
  if (!res.ok) throw toError(res.status, await res.json().catch(() => undefined))
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// ---- chat (RAG) ----
export interface ChatSource {
  n: number
  chunk_id: string
  document_id: string
  document_name: string
  label: string
  excerpt: string
  method: 'hybrid' | 'keyword' | 'vector'
  scores: { fused: number; keyword_rank: number | null; vector_rank: number | null }
}
export interface RetrievalEvent {
  query_used: string
  rewritten: boolean
  meta: {
    methods_used: string[]
    semantic: boolean
    embedding_model: string
    degraded: string | null
    latency_ms: number
    candidates: { keyword: number; vector: number }
    llm_model: string
    generative: boolean
  }
  sources: ChatSource[]
}
export interface DoneEvent {
  cited: number[]
  grounded: boolean
  reason: string | null
}
export interface ChatHandlers {
  onRetrieval: (e: RetrievalEvent) => void
  onToken: (text: string) => void
  onDone: (e: DoneEvent) => void
  onError: (e: { code: string; message: string }) => void
}
export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
}

/** POST + server-sent events over fetch (EventSource cannot send an Authorization header). */
export async function streamChat(
  messages: ChatTurn[],
  opts: { topK: number; documentIds?: string[] },
  h: ChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken()
  let res: Response
  try {
    res = await fetch(`${API_BASE_URL}/v1/chat`, {
      method: 'POST',
      signal,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      },
      body: JSON.stringify({
        messages,
        top_k: opts.topK,
        document_ids: opts.documentIds?.length ? opts.documentIds : undefined,
      }),
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') return
    throw new ApiError(0, 'Unable to reach the PAW service.')
  }
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    throw toError(res.status, await res.json().catch(() => undefined))
  }
  if (!res.body) throw new ApiError(0, 'The response could not be streamed.')
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let i: number
      while ((i = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, i)
        buf = buf.slice(i + 2)
        const ev = /^event: (.+)$/m.exec(frame)?.[1]
        const data = /^data: (.+)$/m.exec(frame)?.[1]
        if (!ev || !data) continue
        const json: unknown = JSON.parse(data)
        if (ev === 'retrieval') h.onRetrieval(json as RetrievalEvent)
        else if (ev === 'token') h.onToken((json as { text: string }).text)
        else if (ev === 'done') h.onDone(json as DoneEvent)
        else if (ev === 'error') h.onError(json as { code: string; message: string })
      }
    }
  } catch (e) {
    if ((e as Error).name !== 'AbortError') throw new ApiError(0, 'The answer stream was interrupted.')
  }
}

/** Dev-only: the server rejects this outside local/ci environments. */
export const clearAllData = () => request<{ documents_deleted: number }>('/v1/dev/data', { method: 'DELETE' })

export const deleteDocument = (id: string) => request<void>(`/v1/documents/${id}`, { method: 'DELETE' })
