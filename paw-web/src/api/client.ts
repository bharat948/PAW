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

function isEnvelope(x: unknown): x is ErrorEnvelope {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as ErrorEnvelope).code === 'string' &&
    typeof (x as ErrorEnvelope).message === 'string'
  )
}

async function request<T>(path: string, init?: RequestInit, base = API_BASE_URL): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${base}${path}`, { ...init, headers: { Accept: 'application/json' } })
  } catch {
    // Network failure / CORS / server down: no envelope available.
    throw new ApiError(0, 'Unable to reach the PAW service.')
  }
  const body: unknown = await res.json().catch(() => undefined)
  if (!res.ok) {
    const env = isEnvelope(body) ? body : undefined
    throw new ApiError(res.status, env?.message ?? `Request failed (${res.status}).`, env)
  }
  return body as T
}

export interface Readiness {
  status: 'ready'
}

export const getReady = (base?: string) => request<Readiness>('/ready', undefined, base)
