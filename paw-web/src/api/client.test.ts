import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getReady } from './client'

afterEach(() => vi.restoreAllMocks())

const mockFetch = (res: Response | Error) =>
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    res instanceof Error ? Promise.reject(res) : Promise.resolve(res),
  )

describe('getReady', () => {
  it('returns body on 200', async () => {
    mockFetch(Response.json({ status: 'ready' }))
    await expect(getReady('http://x')).resolves.toEqual({ status: 'ready' })
  })

  it('parses the error envelope on 503', async () => {
    const env = { code: 'not_ready', message: 'nope', correlation_id: 'c1', retryable: true }
    mockFetch(Response.json(env, { status: 503 }))
    const err = await getReady('http://x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(503)
    expect((err as ApiError).envelope?.correlation_id).toBe('c1')
  })

  it('maps network failure to status 0', async () => {
    mockFetch(new TypeError('failed'))
    const err = (await getReady('http://x').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(0)
  })
})
