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
  let res: Response
  try {
    res = await fetch(`${opts.base ?? API_BASE_URL}${path}`, { ...init, headers })
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
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
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
export const getChunk = (id: string) => request<ChunkDetail>(`/v1/chunks/${id}`)

/** Authenticated download (the endpoint needs the bearer header, so a plain link would fail). */
export async function downloadOriginal(originalUrl: string, filename: string): Promise<void> {
  const token = getToken()
  let res: Response
  try {
    res = await fetch(`${API_BASE_URL}${originalUrl}`, {
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
