import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { setCsrf, setToken, type ProfileSpec } from './api/client'
import { diffSpecs } from './lib/specDiff'

const session = { mode: 'session', email: 'o@example.org', csrf_token: 'csrf-abc', expires_at: 'x', authenticated_at: 'x', stepup_valid: true }
const spec = (over: Partial<ProfileSpec> = {}): ProfileSpec => ({
  persona: { title: 'PAW Admin', summary: 'Helps Bharat.', expertise: ['scheduling'], communication: { style: 'concise', uncertainty: 'explicit' } },
  ethos: ['Be honest.'],
  runtime: { harness: 'hermes', model: 'gpt-4o-mini', max_iterations: 25, max_run_seconds: 600 },
  tools: { grants: ['calendar_list_events', 'docs_search'] },
  budgets: { per_run_usd: 0.5, daily_usd: 3 },
  ...over,
})
const version = (n: number, status: string, s = spec()) => ({
  id: `v${n}`, version: n, status, spec: s, compiled_hash: `h${n}`, created_by: 'owner', created_at: '2026-11-03T10:00:00Z',
  activated_at: status === 'draft' ? null : '2026-11-03T11:00:00Z',
})
const view = (versions = [version(1, 'active')], restart = false) => ({
  runtime_config: restart ? 'mismatch' : 'matches', restart_required: restart,
  actor_id: 'admin-hermes', active: versions.find((v) => v.status === 'active') ?? null, versions,
  known_tools: ['calendar_list_events', 'docs_get_source', 'docs_search', 'paw_run_status', 'paw_system_status'],
})
const envelope = (code: string) => ({ code, message: 'm', correlation_id: 'c', retryable: false })

type Handler = (url: string, init?: RequestInit) => Response | undefined
function api(handlers: Handler[]) {
  const calls: { url: string; method: string; body?: any }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    calls.push({ url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : undefined })
    for (const h of handlers) {
      const r = h(url, init)
      if (r) return r
    }
    return Response.json(envelope('unauthenticated'), { status: 401 })
  })
  return calls
}
const base: Handler = (url) => {
  if (url.includes('/v1/auth/config')) return Response.json({ mode: 'google', google: true })
  if (url.includes('/v1/auth/me')) return Response.json(session)
}
const getProfile = (v = view()): Handler => (url, init) => (url.endsWith('/v1/agent/profile') && !init?.method ? Response.json(v) : undefined)

beforeEach(() => {
  setToken(null)
  setCsrf(null)
  window.history.replaceState({}, '', '/#/profile')
})
afterEach(() => vi.restoreAllMocks())

it('shows the active version in an editable form with the versions list', async () => {
  api([getProfile(), base])
  render(<App />)
  expect(await screen.findByLabelText('Title')).toHaveValue('PAW Admin')
  expect(screen.getByLabelText(/rules/i)).toHaveValue('Be honest.')
  expect(screen.getByLabelText('Model')).toHaveValue('gpt-4o-mini')
  expect(screen.getByRole('checkbox', { name: 'calendar_list_events' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'paw_run_status' })).not.toBeChecked()
  const list = screen.getByRole('list', { name: 'Versions' })
  expect(within(list).getByText('Version 1')).toBeInTheDocument()
  expect(within(list).getByText('active')).toBeInTheDocument()
})

it('a persona-only change saves a draft and activates without any confirmation box', async () => {
  const calls = api([
    (u, i) => (u.endsWith('/versions') && i?.method === 'POST' ? Response.json({ id: 'v2', version: 2, status: 'draft', changes: [], requires_confirmation: false }, { status: 201 }) : undefined),
    (u, i) => (u.endsWith('/v2/activate') && i?.method === 'POST' ? Response.json({ version: 2, compiled_hash: 'h2', changes: [], restart_required: false }) : undefined),
    getProfile(),
    base,
  ])
  render(<App />)
  const rules = await screen.findByLabelText(/rules/i)
  await userEvent.clear(rules)
  await userEvent.type(rules, 'Always greet warmly.')
  await userEvent.click(screen.getByRole('button', { name: /save as new version/i }))
  const region = await screen.findByRole('region', { name: /new version/i })
  expect(within(region).getByText(/Rules/)).toBeInTheDocument()
  expect(within(region).queryByRole('checkbox')).not.toBeInTheDocument()
  expect(JSON.parse(JSON.stringify(calls.find((c) => c.url.endsWith('/versions'))!.body)).spec.ethos).toEqual(['Always greet warmly.'])
  await userEvent.click(within(region).getByRole('button', { name: /activate version 2/i }))
  expect(await screen.findByText(/version 2 is now active/i)).toBeInTheDocument()
  expect(screen.getByText(/new messages use it right away/i)).toBeInTheDocument()
  expect(calls.find((c) => c.url.endsWith('/v2/activate'))!.body).toEqual({ confirm: false })
})

it('a tool change needs the confirmation box, then says Hermès must be restarted', async () => {
  const calls = api([
    (u, i) => (u.endsWith('/versions') && i?.method === 'POST' ? Response.json({ id: 'v2', version: 2, status: 'draft', changes: ['tools'], requires_confirmation: true }, { status: 201 }) : undefined),
    (u, i) => (u.endsWith('/v2/activate') && i?.method === 'POST' ? Response.json({ version: 2, compiled_hash: 'h2', changes: ['tools'], restart_required: true }) : undefined),
    getProfile(),
    base,
  ])
  render(<App />)
  await userEvent.click(await screen.findByRole('checkbox', { name: 'paw_system_status' }))
  await userEvent.click(screen.getByRole('button', { name: /save as new version/i }))
  const region = await screen.findByRole('region', { name: /new version/i })
  expect(within(region).getByText(/calendar_list_events, docs_search → calendar_list_events, docs_search, paw_system_status/)).toBeInTheDocument()
  const activate = within(region).getByRole('button', { name: /activate version 2/i })
  expect(activate).toBeDisabled()
  await userEvent.click(within(region).getByRole('checkbox', { name: /changes the tools it may use/i }))
  expect(activate).toBeEnabled()
  await userEvent.click(activate)
  expect(await screen.findByText(/restart the hermès container/i)).toBeInTheDocument()
  expect(calls.find((c) => c.url.endsWith('/v2/activate'))!.body).toEqual({ confirm: true })
})

it('asks the owner to sign in again when the step-up window has passed', async () => {
  api([
    (u, i) => (u.endsWith('/versions') && i?.method === 'POST' ? Response.json({ id: 'v2', version: 2, status: 'draft', changes: [], requires_confirmation: false }, { status: 201 }) : undefined),
    (u, i) => (u.endsWith('/v2/activate') && i?.method === 'POST' ? Response.json(envelope('stepup_required'), { status: 403 }) : undefined),
    getProfile(),
    base,
  ])
  render(<App />)
  await userEvent.type(await screen.findByLabelText('Title'), ' X')
  await userEvent.click(screen.getByRole('button', { name: /save as new version/i }))
  await userEvent.click(await screen.findByRole('button', { name: /activate version 2/i }))
  const link = await screen.findByRole('link', { name: /sign in again/i })
  expect(link).toHaveAttribute('href', expect.stringContaining('purpose=stepup'))
})

it('shows the server message for an unknown tool and a generic one for other validation errors', async () => {
  api([
    (u, i) => (u.endsWith('/versions') && i?.method === 'POST' ? Response.json({ ...envelope('validation_error'), message: 'x' }, { status: 422 }) : undefined),
    getProfile(),
    base,
  ])
  render(<App />)
  await userEvent.click(await screen.findByRole('button', { name: /save as new version/i }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/not valid/i)
})

it('compares an older version with the active one field by field', async () => {
  const old = version(1, 'retired', spec({ budgets: { per_run_usd: 0.1, daily_usd: 1 } }))
  const cur = version(2, 'active')
  api([getProfile(view([cur, old])), base])
  render(<App />)
  await userEvent.click(await screen.findByRole('button', { name: /compare with active/i }))
  const diff = await screen.findByRole('list', { name: /version 1 compared with active/i })
  expect(diff).toHaveTextContent('Per-run budget (USD): active 0.5 · this version 0.1')
  expect(diff).toHaveTextContent('Daily budget (USD): active 3 · this version 1')
  expect(diff).not.toHaveTextContent('Model')
})

it('can be retried when the profile fails to load', async () => {
  api([(u) => (u.endsWith('/v1/agent/profile') ? Response.json(envelope('internal_error'), { status: 500 }) : undefined), base])
  render(<App />)
  expect(await screen.findByText(/could not load the profile/i)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
})

it('diffSpecs ignores tool order and reports only real differences', () => {
  const a = spec()
  expect(diffSpecs(a, spec({ tools: { grants: ['docs_search', 'calendar_list_events'] } }))).toEqual([])
  expect(diffSpecs(a, spec({ ethos: ['A', 'B'] }))).toEqual([{ field: 'Rules', before: 'Be honest.', after: 'A\nB' }])
  expect(diffSpecs(a, spec({ tools: { grants: [] } }))).toEqual([{ field: 'Tools', before: 'calendar_list_events, docs_search', after: '—' }])
})

it('keeps telling the owner to restart Hermès while its container runs an older configuration', async () => {
  api([getProfile(view([version(1, 'active')], true)), base])
  render(<App />)
  expect(await screen.findByText(/older tool list or model/i)).toBeInTheDocument()
})

it('shows no restart warning when the container matches', async () => {
  api([getProfile(), base])
  render(<App />)
  await screen.findByLabelText('Title')
  expect(screen.queryByText(/older tool list or model/i)).not.toBeInTheDocument()
})
