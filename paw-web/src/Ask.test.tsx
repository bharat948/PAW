import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { setToken } from './api/client'

const enc = new TextEncoder()
const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`

/** A real ReadableStream, delivered in awkward chunk boundaries to exercise the SSE parser. */
function sse(frames: string[]) {
  const bytes = enc.encode(frames.join(''))
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      for (let i = 0; i < bytes.length; i += 23) c.enqueue(bytes.slice(i, i + 23))
      c.close()
    },
  })
  return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
}

const source = (n = 1) => ({
  n, chunk_id: `c${n}`, document_id: 'd1', document_name: 'cooking.txt', label: 'cooking.txt', method: 'hybrid',
  excerpt: 'Feed the starter daily with flour', scores: { fused: 0.03, keyword_rank: 1, vector_rank: 1 },
})
const retrieval = (over = {}) => ({
  query_used: 'sourdough starter', rewritten: false,
  meta: { methods_used: ['keyword', 'vector'], semantic: true, embedding_model: 'm', degraded: null, latency_ms: 12,
    candidates: { keyword: 1, vector: 1 }, llm_model: 'gpt-x', generative: true },
  sources: [source()], ...over,
})
const chunk = { chunk_id: 'c1', document_id: 'd1', document_version_id: 'v1', document_name: 'cooking.txt', media_type: 'text/plain',
  ordinal: 0, text: 'Full cooking passage', page_number: null, section: null, source_start: 0, source_end: 9, metadata: {},
  is_current_version: true, original_url: '/o', previous_chunk_id: null, next_chunk_id: null }

beforeEach(() => {
  setToken('t'.repeat(20))
  window.location.hash = '#/ask'
})
afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
})

async function ask(q = 'how do I feed the starter?') {
  render(<App />)
  await userEvent.type(screen.getByLabelText('Ask a question'), q)
  await userEvent.click(screen.getByRole('button', { name: /send/i }))
}

it('streams an answer with clickable citations that open the source', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation((u) =>
    Promise.resolve(
      String(u).includes('/v1/chunks/')
        ? Response.json(chunk)
        : sse([
            frame('retrieval', retrieval()),
            frame('token', { text: 'Feed it daily ' }),
            frame('token', { text: 'with flour [1]. Also see [7].' }),
            frame('done', { cited: [1], grounded: true, reason: null }),
          ]),
    ),
  )
  await ask()
  expect(await screen.findByText(/Feed it daily/)).toBeInTheDocument()
  const cite = await screen.findByRole('button', { name: 'Source 1' })
  expect(screen.queryByRole('button', { name: 'Source 7' })).toBeNull() // out-of-range marker stays plain text
  await userEvent.click(cite)
  expect(await screen.findByText('Full cooking passage')).toBeInTheDocument()
})

it('shows the retrieval trace: query used, rewrite, methods and cited flags', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      sse([
        frame('retrieval', retrieval({ query_used: 'standalone rewritten query', rewritten: true })),
        frame('token', { text: 'Answer [1]' }),
        frame('done', { cited: [1], grounded: true, reason: null }),
      ]),
    ),
  )
  await ask()
  const trace = (await screen.findByText(/Retrieval trace/)).closest('details')!
  expect(within(trace).getByText('standalone rewritten query')).toBeInTheDocument()
  expect(within(trace).getByText(/rewritten from your follow-up/)).toBeInTheDocument()
  expect(within(trace).getByText('cited')).toBeInTheDocument()
  expect(within(trace).getByText('hybrid')).toBeInTheDocument()
})

it('renders model output as inert text, never HTML', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      sse([
        frame('retrieval', retrieval()),
        frame('token', { text: 'Hi <img src=x onerror="window.__pwned=1"> <b>bold</b> [1]' }),
        frame('done', { cited: [1], grounded: true, reason: null }),
      ]),
    ),
  )
  await ask()
  expect(await screen.findByText(/<img src=x/)).toBeInTheDocument()
  expect(document.querySelector('.bubble img, .bubble b')).toBeNull()
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
})

it('flags uncited answers, offline mode, and no-source replies', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      sse([
        frame('retrieval', retrieval({ sources: [], meta: { ...retrieval().meta, generative: false } })),
        frame('token', { text: "I couldn't find anything relevant in your documents for that question." }),
        frame('done', { cited: [], grounded: false, reason: 'no_sources' }),
      ]),
    ),
  )
  await ask('nonsense')
  expect(await screen.findByText(/couldn't find anything relevant/)).toBeInTheDocument()
  expect(screen.getByText(/Offline demo mode/)).toBeInTheDocument()
  expect(screen.getByText(/model was not called/)).toBeInTheDocument()
  expect(screen.queryByText(/did not cite any source/)).toBeNull() // no_sources is not a "forgot to cite" case
})

it('surfaces stream errors and HTTP errors safely', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementationOnce(() =>
    Promise.resolve(
      sse([
        frame('retrieval', retrieval()),
        frame('error', { code: 'llm_unavailable', message: 'The language model is busy or unavailable. Please try again.' }),
      ]),
    ),
  )
  await ask()
  expect(await screen.findByRole('alert')).toHaveTextContent(/busy or unavailable/)
})

it('maps 429 to a friendly message', async () => {
  const env = { code: 'rate_limited', message: 'x', correlation_id: 'c', retryable: true }
  vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(Response.json(env, { status: 429 })))
  await ask()
  expect(await screen.findByRole('alert')).toHaveTextContent(/too many questions/i)
})

it('sends prior turns as history on follow-ups', async () => {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      sse([frame('retrieval', retrieval()), frame('token', { text: 'ok [1]' }), frame('done', { cited: [1], grounded: true, reason: null })]),
    ),
  )
  await ask('first question')
  await screen.findByRole('button', { name: /send/i })
  await userEvent.type(screen.getByLabelText('Ask a question'), 'and then?')
  await userEvent.click(screen.getByRole('button', { name: /send/i }))
  await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  const body = JSON.parse((spy.mock.calls[1][1] as RequestInit).body as string)
  expect(body.messages.map((m: { role: string }) => m.role)).toEqual(['user', 'assistant', 'user'])
  expect(body.messages.at(-1).content).toBe('and then?')
})
