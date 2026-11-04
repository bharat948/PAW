import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { setCsrf, setToken } from './api/client'

const session = { mode: 'session', email: 'o@example.org', csrf_token: 'csrf-abc', expires_at: 'x', authenticated_at: 'x', stepup_valid: true }
const envelope = (code: string) => ({ code, message: 'x', correlation_id: 'c', retryable: false })
const run = (over: Record<string, unknown> = {}) => ({
  id: 'run-1', session_id: 'sess-1', client_request_id: 'c1', message: 'hello', state: 'QUEUED', output: null,
  error_code: null, usage: null, queued_at: 't', started_at: null, finished_at: null, ...over,
})
const ev = (seq: number, type: string, payload: Record<string, unknown> = {}) => ({ seq, type, payload })
const sse = (events: ReturnType<typeof ev>[]) =>
  new Response(events.map((e) => `id: ${e.seq}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join(''), {
    headers: { 'Content-Type': 'text/event-stream' },
  })

type Handler = (url: string, init?: RequestInit) => Response | undefined
function api(handlers: Handler[]) {
  const calls: { url: string; method: string; body?: string }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    calls.push({ url, method: init?.method ?? 'GET', body: init?.body as string | undefined })
    for (const h of handlers) {
      const r = h(url, init)
      if (r) return r
    }
    return Response.json(envelope('unauthenticated'), { status: 401 })
  })
  return calls
}
const base: Handler = (url) => {
  if (url.endsWith('/v1/agent-sessions')) return Response.json({ sessions: [] })
  if (url.includes('/messages?')) return Response.json({ messages: [], next_after: null, summaries: [], children: [], summary_job: null })
  if (url.includes('/v1/auth/config')) return Response.json({ mode: 'google', google: true })
  if (url.includes('/v1/auth/me')) return Response.json(session)
  if (url.includes('/v1/runtime/health')) return Response.json({ configured: true, ready: true, missing: [] })
}

beforeEach(() => {
  setToken(null)
  setCsrf(null)
  window.localStorage.clear()
  window.history.replaceState({}, '', '/#/agent')
})
afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
  setCsrf(null)
})

it('says so when Admin Hermès is not configured and disables sending', async () => {
  api([(u) => (u.includes('/v1/runtime/health') ? Response.json({ configured: false, ready: false, missing: ['not_configured'] }) : undefined), base])
  render(<App />)
  expect(await screen.findByText(/not configured on this server/i)).toBeInTheDocument()
  expect(screen.getByLabelText(/message admin hermès/i)).toBeDisabled()
})

it('warns when the runtime is configured but not ready', async () => {
  api([(u) => (u.includes('/v1/runtime/health') ? Response.json({ configured: true, ready: false, missing: ['run_stop'] }) : undefined), base])
  render(<App />)
  expect(await screen.findByText(/not ready \(run_stop\)/i)).toBeInTheDocument()
})

it('creates a conversation on the first message, streams the answer and shows the stored output', async () => {
  const calls = api([
    (u, i) => (u.endsWith('/v1/agent-sessions') && i?.method === 'POST' ? Response.json({ id: 'sess-1' }, { status: 201 }) : undefined),
    (u, i) => (u.endsWith('/v1/agent-sessions/sess-1/runs') && i?.method === 'POST' ? Response.json(run(), { status: 202 }) : undefined),
    (u) => (u.includes('/v1/agent-runs/run-1/events')
      ? sse([ev(1, 'run.queued'), ev(2, 'run.state', { state: 'RUNNING' }), ev(3, 'message.delta', { text: 'Hi ' }), ev(4, 'message.delta', { text: 'there' }), ev(5, 'run.completed')])
      : undefined),
    (u) => (u.endsWith('/v1/agent-runs/run-1') ? Response.json(run({ state: 'SUCCEEDED', output: 'Hi there', usage: { input_tokens: 3, output_tokens: 2 } })) : undefined),
    base,
  ])
  render(<App />)
  await userEvent.type(await screen.findByLabelText(/message admin hermès/i), 'hello')
  await userEvent.click(screen.getByRole('button', { name: /send/i }))
  expect(await screen.findByText('Hi there')).toBeInTheDocument()
  const post = calls.find((c) => c.url.endsWith('/sess-1/runs'))
  expect(JSON.parse(post!.body!)).toMatchObject({ message: 'hello' })
  expect(JSON.parse(post!.body!).client_request_id).toMatch(/\S{8,}/)
  expect(window.localStorage.getItem('paw.agent.session')).toBe('sess-1')
  await waitFor(() => expect(screen.getByLabelText(/message admin hermès/i)).toBeEnabled())
})

it('shows tool activity as it happens', async () => {
  api([
    (u, i) => (u.endsWith('/v1/agent-sessions') && i?.method === 'POST' ? Response.json({ id: 'sess-1' }, { status: 201 }) : undefined),
    (u, i) => (u.endsWith('/runs') && i?.method === 'POST' ? Response.json(run(), { status: 202 }) : undefined),
    (u) => (u.includes('/events') ? sse([ev(1, 'tool.started', { tool: 'calendar_list_events' }), ev(2, 'tool.completed', { tool: 'calendar_list_events', error: false }), ev(3, 'run.completed')]) : undefined),
    (u) => (u.endsWith('/v1/agent-runs/run-1') ? Response.json(run({ state: 'SUCCEEDED', output: 'You are free.' })) : undefined),
    base,
  ])
  render(<App />)
  await userEvent.type(await screen.findByLabelText(/message admin hermès/i), 'what is on my calendar?')
  await userEvent.click(screen.getByRole('button', { name: /send/i }))
  const list = await screen.findByRole('list', { name: /tool activity/i })
  expect(list).toHaveTextContent('calendar_list_events')
  expect(list).toHaveTextContent('done')
})

it('picks a run back up after the page was closed mid-answer', async () => {
  window.localStorage.setItem('paw.agent.session', 'sess-1')
  const calls = api([
    (u) => (u.endsWith('/v1/agent-sessions/sess-1/runs') ? Response.json({ runs: [run({ state: 'RUNNING' })] }) : undefined),
    (u) => (u.includes('/events') ? sse([ev(1, 'run.queued'), ev(2, 'message.delta', { text: 'Still ' }), ev(3, 'message.delta', { text: 'going' }), ev(4, 'run.completed')]) : undefined),
    (u) => (u.endsWith('/v1/agent-runs/run-1') ? Response.json(run({ state: 'SUCCEEDED', output: 'Still going' })) : undefined),
    base,
  ])
  render(<App />)
  expect(await screen.findByText('hello')).toBeInTheDocument()
  expect(await screen.findByText('Still going')).toBeInTheDocument()
  expect(calls.some((c) => c.url.includes('/events?after=0'))).toBe(true)
})

it('shows finished history from a remembered conversation without streaming', async () => {
  window.localStorage.setItem('paw.agent.session', 'sess-1')
  const calls = api([
    (u) => (u.endsWith('/v1/agent-sessions/sess-1/runs') ? Response.json({ runs: [run({ state: 'SUCCEEDED', output: 'An old answer' })] }) : undefined),
    base,
  ])
  render(<App />)
  expect(await screen.findByText('An old answer')).toBeInTheDocument()
  expect(calls.some((c) => c.url.includes('/events'))).toBe(false)
})

it('reconnects from the last sequence number when the stream drops, with no duplicate text', async () => {
  window.localStorage.setItem('paw.agent.session', 'sess-1')
  let streams = 0
  const calls = api([
    (u) => (u.endsWith('/v1/agent-sessions/sess-1/runs') ? Response.json({ runs: [run({ state: 'RUNNING' })] }) : undefined),
    (u) => {
      if (!u.includes('/events')) return undefined
      streams += 1
      return streams === 1
        ? sse([ev(1, 'run.queued'), ev(2, 'message.delta', { text: 'one ' })])
        : sse([ev(2, 'message.delta', { text: 'one ' }), ev(3, 'message.delta', { text: 'two' }), ev(4, 'run.completed')])
    },
    (u) => (u.endsWith('/v1/agent-runs/run-1') ? Response.json(run({ state: streams >= 2 ? 'SUCCEEDED' : 'RUNNING', output: streams >= 2 ? 'one two' : null })) : undefined),
    base,
  ])
  render(<App />)
  expect(await screen.findByText('one two', {}, { timeout: 4000 })).toBeInTheDocument()
  expect(calls.filter((c) => c.url.includes('/events')).map((c) => c.url.split('after=')[1])).toEqual(['0', '2'])
})

it('stops a running answer', async () => {
  window.localStorage.setItem('paw.agent.session', 'sess-1')
  const calls = api([
    (u) => (u.endsWith('/v1/agent-sessions/sess-1/runs') ? Response.json({ runs: [run({ state: 'RUNNING' })] }) : undefined),
    (u) => (u.includes('/events') ? new Response(new ReadableStream({ start() {} }), { headers: { 'Content-Type': 'text/event-stream' } }) : undefined),
    (u, i) => (u.endsWith('/v1/agent-runs/run-1/cancel') && i?.method === 'POST' ? Response.json(run({ state: 'CANCELLING' })) : undefined),
    base,
  ])
  render(<App />)
  await userEvent.click(await screen.findByRole('button', { name: /stop/i }))
  expect(await screen.findByText(/stopping/i)).toBeInTheDocument()
  expect(calls.some((c) => c.url.endsWith('/cancel') && c.method === 'POST')).toBe(true)
  expect(screen.queryByRole('button', { name: /^stop$/i })).not.toBeInTheDocument()
})

it('keeps the partial answer and explains an interrupted run', async () => {
  window.localStorage.setItem('paw.agent.session', 'sess-1')
  api([
    (u) => (u.endsWith('/v1/agent-sessions/sess-1/runs') ? Response.json({ runs: [run({ state: 'INTERRUPTED', output: 'half an ans', error_code: 'stream_lost' })] }) : undefined),
    base,
  ])
  render(<App />)
  expect(await screen.findByText('half an ans')).toBeInTheDocument()
  expect(screen.getByText(/interrupted/i)).toBeInTheDocument()
  expect(screen.queryByText('SUCCEEDED')).not.toBeInTheDocument()
})

it('shows a friendly message when the agent is busy or the conversation already has a run', async () => {
  const bad = (status: number, code: string): Handler => (u, i) =>
    u.endsWith('/runs') && i?.method === 'POST' ? Response.json(envelope(code), { status }) : undefined
  for (const [status, code, text] of [[429, 'queue_full', /busy right now/i], [409, 'session_busy', /previous message/i]] as const) {
    vi.restoreAllMocks()
    window.localStorage.setItem('paw.agent.session', 'sess-1')
    api([bad(status, code), (u) => (u.endsWith('/sess-1/runs') && !u.includes('POST') ? Response.json({ runs: [] }) : undefined), base])
    const { unmount } = render(<App />)
    await userEvent.type(await screen.findByLabelText(/message admin hermès/i), 'hi')
    await userEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(await screen.findByText(text)).toBeInTheDocument()
    expect(screen.getByLabelText(/message admin hermès/i)).toHaveValue('hi') // the draft is not lost
    unmount()
  }
})

it('starts fresh when the remembered conversation no longer exists', async () => {
  window.localStorage.setItem('paw.agent.session', 'gone')
  api([(u) => (u.includes('/agent-sessions/gone/runs') ? Response.json(envelope('not_found'), { status: 404 }) : undefined), base])
  render(<App />)
  expect(await screen.findByText(/ask something to get started/i)).toBeInTheDocument()
  expect(window.localStorage.getItem('paw.agent.session')).toBeNull()
})

it('New conversation forgets the current one', async () => {
  window.localStorage.setItem('paw.agent.session', 'sess-1')
  api([(u) => (u.endsWith('/v1/agent-sessions/sess-1/runs') ? Response.json({ runs: [run({ state: 'SUCCEEDED', output: 'old' })] }) : undefined), base])
  render(<App />)
  await screen.findByText('old')
  await userEvent.click(screen.getByRole('button', { name: /new conversation/i }))
  expect(screen.queryByText('old')).not.toBeInTheDocument()
  expect(window.localStorage.getItem('paw.agent.session')).toBeNull()
})

const withRun = (events: ReturnType<typeof ev>[]): Handler[] => [
  (u, i) => (u.endsWith('/v1/agent-sessions') && i?.method === 'POST' ? Response.json({ id: 'sess-1' }, { status: 201 }) : undefined),
  (u, i) => (u.endsWith('/runs') && i?.method === 'POST' ? Response.json(run(), { status: 202 }) : undefined),
  (u) => (u.includes('/events') ? sse(events) : undefined),
  (u) => (u.endsWith('/v1/agent-runs/run-1') ? Response.json(run({ state: 'SUCCEEDED', output: 'Done.' })) : undefined),
  base,
]
async function sendOne(text: string) {
  render(<App />)
  await userEvent.type(await screen.findByLabelText(/message admin hermès/i), text)
  await userEvent.click(screen.getByRole('button', { name: /send/i }))
}

it('shows what the gateway decided for each tool call, with plain reasons', async () => {
  api(withRun([
    ev(1, 'tool.decision', { tool: 'docs_search', decision: 'allowed', reason: 'OK' }),
    ev(2, 'tool.decision', { tool: 'calendar_list_events', decision: 'denied', reason: 'NOT_CONNECTED' }),
    ev(3, 'tool.decision', { tool: 'terminal', decision: 'denied', reason: 'NATIVE_TOOL_BLOCKED' }),
    ev(4, 'tool.decision', { tool: 'run_shell_command', decision: 'denied', reason: 'UNKNOWN_TOOL' }),
    ev(5, 'run.completed'),
  ]))
  await sendOne('what is on my calendar?')
  const list = await screen.findByRole('list', { name: /tool activity/i })
  expect(list).toHaveTextContent('docs_search allowed')
  expect(list).toHaveTextContent('calendar_list_events denied: not connected')
  expect(list).toHaveTextContent('terminal blocked: not a PAW tool')
  expect(list).toHaveTextContent('run_shell_command denied: no such tool')
  expect(within(list).getAllByRole('listitem')[1]).toHaveClass('tool-denied')
  expect(within(list).getAllByRole('listitem')[0]).not.toHaveClass('tool-denied')
})

it('prefers the gateway decisions over the model-side tool events (no double counting)', async () => {
  api(withRun([
    ev(1, 'tool.started', { tool: 'docs_search' }),
    ev(2, 'tool.decision', { tool: 'docs_search', decision: 'allowed', reason: 'OK' }),
    ev(3, 'tool.completed', { tool: 'docs_search', error: false }),
    ev(4, 'run.completed'),
  ]))
  await sendOne('search')
  const list = await screen.findByRole('list', { name: /tool activity/i })
  expect(within(list).getAllByRole('listitem')).toHaveLength(1)
  expect(list).toHaveTextContent('docs_search allowed')
})

it('names an unknown reason code instead of hiding it', async () => {
  api(withRun([ev(1, 'tool.decision', { tool: 'x_tool', decision: 'denied', reason: 'SOMETHING_NEW' }), ev(2, 'run.completed')]))
  await sendOne('go')
  expect(await screen.findByRole('list', { name: /tool activity/i })).toHaveTextContent('x_tool denied: SOMETHING_NEW')
})

it('tells the owner to restart Hermès when its container does not match the profile', async () => {
  api([(u) => (u.includes('/v1/runtime/health') ? Response.json({ configured: true, ready: false, missing: ['config_mismatch'], config: 'mismatch' }) : undefined), base])
  render(<App />)
  expect(await screen.findByText(/does not match its profile/i)).toBeInTheDocument()
})
