import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { setCsrf, setToken } from './api/client'

const session = { mode: 'session', email: 'o@example.org', csrf_token: 'csrf-abc', expires_at: 'x', authenticated_at: 'x', stepup_valid: true }
const docs = { id: 'documents', name: 'PAW Documents', description: 'Search docs.', kind: 'built_in', state: 'connected', tools: [{ name: 'docs_search', access: 'read' }] }
const cal = (extra = {}) => ({
  id: 'google_calendar', name: 'Google Calendar', description: 'Read events.', kind: 'connector', state: 'available',
  tools: [{ name: 'calendar_list_events', access: 'read' }], ...extra,
})

function api(routes: Record<string, (init?: RequestInit) => Response>) {
  const calls: { url: string; method: string; csrf: string | null }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    calls.push({ url, method: init?.method ?? 'GET', csrf: new Headers(init?.headers).get('X-CSRF-Token') })
    const hit = Object.keys(routes).find((k) => url.includes(k))
    return hit ? routes[hit](init) : Response.json({ code: 'unauthenticated', message: 'x', correlation_id: 'c', retryable: false }, { status: 401 })
  })
  return calls
}
// first match wins, so specific overrides come before the generic '/v1/integrations'
const base = (calendar: object, more: Record<string, (i?: RequestInit) => Response> = {}) => ({
  ...more,
  '/v1/auth/config': () => Response.json({ mode: 'google', google: true }),
  '/v1/auth/me': () => Response.json(session),
  '/v1/integrations/google-calendar/events': () => Response.json({ account_email: 'c@example.org', events: [{ id: '1', title: 'Standup', start: '2026-11-04T09:00:00Z', end: null, all_day: false, location: 'Room 1' }] }),
  '/v1/integrations': () => Response.json({ integrations: [docs, calendar] }),
})

beforeEach(() => {
  setToken(null)
  setCsrf(null)
  window.history.replaceState({}, '', '/#/integrations')
})
afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
  setCsrf(null)
})

it('offers a Connect link to the server connect endpoint when not connected', async () => {
  api(base(cal()))
  render(<App />)
  const link = await screen.findByRole('link', { name: /connect google calendar/i })
  expect(link).toHaveAttribute('href', expect.stringContaining('/v1/integrations/google-calendar/connect?return_to='))
  expect(screen.getByText('calendar_list_events')).toBeInTheDocument()
  expect(screen.getByText('PAW Documents')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /disconnect/i })).not.toBeInTheDocument()
})

it('shows the account and upcoming events when connected', async () => {
  api(base(cal({ state: 'connected', account_email: 'c@example.org' })))
  render(<App />)
  expect(await screen.findByText('c@example.org')).toBeInTheDocument()
  expect(await screen.findByText('Standup')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /connect google calendar/i })).not.toBeInTheDocument()
})

it('disconnects with the CSRF token after confirmation and refreshes', async () => {
  let connected = true
  const calls = api(base(cal({ state: 'connected', account_email: 'c@example.org' }), {
    '/v1/integrations/google-calendar': () => { connected = false; return new Response(null, { status: 204 }) },
  }))
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<App />)
  await userEvent.click(await screen.findByRole('button', { name: /^disconnect$/i }))
  await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
  expect(calls.find((c) => c.method === 'DELETE')?.csrf).toBe('csrf-abc')
  expect(connected).toBe(false)
})

it('does nothing when the confirmation is declined', async () => {
  const calls = api(base(cal({ state: 'connected', account_email: 'c@example.org' })))
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  render(<App />)
  await userEvent.click(await screen.findByRole('button', { name: /^disconnect$/i }))
  expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
})

it('offers Reconnect when Google access was revoked', async () => {
  api(base(cal({ state: 'needs_reauth', account_email: 'c@example.org' })))
  render(<App />)
  expect(await screen.findByRole('link', { name: /reconnect google calendar/i })).toBeInTheDocument()
  expect(screen.getByText(/needs reconnecting/i)).toBeInTheDocument()
})

it('explains why Calendar is unavailable and offers no connect link', async () => {
  api(base(cal({ state: 'unavailable', reason: 'Needs Google sign-in and PAW_CONNECTOR_KEY to be configured.' })))
  render(<App />)
  expect(await screen.findByText(/PAW_CONNECTOR_KEY/)).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /connect google calendar/i })).not.toBeInTheDocument()
})

it('shows a readable message when Google sends the browser back with an error, then clears it from the URL', async () => {
  window.history.replaceState({}, '', '/?integration_error=missing_scope#/integrations')
  api(base(cal()))
  render(<App />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/calendar permission/i)
  expect(window.location.search).toBe('')
  expect(window.location.hash).toBe('#/integrations')
})

it('shows a retryable error when the catalog cannot load', async () => {
  api({ ...base(cal()), '/v1/integrations': () => Response.json({ code: 'internal_error', message: 'x', correlation_id: 'c', retryable: false }, { status: 500 }) })
  render(<App />)
  expect(await screen.findByText(/could not load integrations/i)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
})
