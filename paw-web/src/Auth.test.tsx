import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { setCsrf, setToken } from './api/client'

const unauth = { code: 'unauthenticated', message: 'Authentication is required.', correlation_id: 'c', retryable: false }
const session = {
  mode: 'session', email: 'owner@example.org', csrf_token: 'csrf-abc', expires_at: 'x',
  authenticated_at: 'x', stepup_valid: true,
}

/** Routes fetch by URL so tests do not depend on call order. */
function api(routes: Record<string, () => Response>) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    const hit = Object.keys(routes).find((k) => url.includes(k))
    return hit ? routes[hit]() : Response.json(unauth, { status: 401 })
  })
}
const cfg = (mode: string, google: boolean) => () => Response.json({ mode, google })

beforeEach(() => {
  setToken(null)
  setCsrf(null)
  window.history.replaceState({}, '', '/#/documents')
})
afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
  setCsrf(null)
})

it('shows only Google when Google is enabled, even in both mode', async () => {
  api({ '/v1/auth/config': cfg('both', true) })
  render(<App />)
  const link = await screen.findByRole('link', { name: /continue with google/i })
  expect(link).toHaveAttribute('href', expect.stringContaining('/v1/auth/google/start?return_to='))
  expect(screen.queryByLabelText(/access token/i)).not.toBeInTheDocument()
})

it('hides the token form in Google-only mode', async () => {
  api({ '/v1/auth/config': cfg('google', true) })
  render(<App />)
  expect(await screen.findByRole('link', { name: /continue with google/i })).toBeInTheDocument()
  expect(screen.queryByLabelText(/access token/i)).not.toBeInTheDocument()
})

it('falls back to the token form when the auth config cannot be read', async () => {
  vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('down'))
  render(<App />)
  expect(await screen.findByLabelText(/owner access token/i)).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /google/i })).not.toBeInTheDocument()
})

it('shows why Google sign-in failed and removes the error from the address bar', async () => {
  window.history.replaceState({}, '', '/?auth_error=not_allowed#/documents')
  api({ '/v1/auth/config': cfg('google', true) })
  render(<App />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/not allowed to sign in/i)
  expect(window.location.search).toBe('')
})

it('treats an unknown error code as a generic failure', async () => {
  window.history.replaceState({}, '', '/?auth_error=%3Cscript%3E#/documents')
  api({ '/v1/auth/config': cfg('google', true) })
  render(<App />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/sign-in failed/i)
  expect(document.querySelector('script')).toBeNull() // the code is never rendered into the page
})

it('recognises an existing Google session on load and signs out with the CSRF token', async () => {
  const spy = api({
    '/v1/auth/me': () => Response.json(session),
    '/v1/documents': () => Response.json({ items: [], next_cursor: null }),
    '/v1/auth/logout': () => new Response(null, { status: 204 }),
    '/v1/auth/config': cfg('google', true),
  })
  render(<App />)
  const out = await screen.findByRole('button', { name: /sign out/i })
  await userEvent.click(out)
  const call = spy.mock.calls.find(([u]) => String(u).includes('/v1/auth/logout'))!
  expect(call[1]?.method).toBe('POST')
  expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe('csrf-abc')
  expect(call[1]?.credentials).toBe('include')
  expect(await screen.findByRole('link', { name: /continue with google/i })).toBeInTheDocument()
})

it('sends the cookie on API calls (credentials include) without a bearer header', async () => {
  const spy = api({
    '/v1/auth/me': () => Response.json(session),
    '/v1/documents': () => Response.json({ items: [], next_cursor: null }),
  })
  render(<App />)
  await screen.findByRole('button', { name: /sign out/i })
  await waitFor(() => expect(spy.mock.calls.some(([u]) => String(u).includes('/v1/documents'))).toBe(true))
  const docs = spy.mock.calls.find(([u]) => String(u).includes('/v1/documents'))!
  expect(docs[1]?.credentials).toBe('include')
  expect(new Headers(docs[1]?.headers).get('Authorization')).toBeNull()
})

it('does not probe for a cookie session when a developer token is already stored', async () => {
  setToken('t'.repeat(20))
  const spy = api({ '/v1/documents': () => Response.json({ items: [], next_cursor: null }) })
  render(<App />)
  await screen.findByRole('button', { name: /sign out/i })
  expect(spy.mock.calls.some(([u]) => String(u).includes('/v1/auth/me'))).toBe(false)
})
