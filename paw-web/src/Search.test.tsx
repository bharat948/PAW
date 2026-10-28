import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { setToken } from './api/client'

const result = (over = {}) => ({
  document_id: 'd1', document_version_id: 'v1', chunk_id: 'c1', document_name: 'markup.txt',
  media_type: 'text/plain', excerpt: 'Widget <script>window.__pwned = 1</script> <b>bold</b>',
  excerpt_truncated: false, retrieval_method: 'hybrid',
  source: { page_number: 2, section: null, source_start: 0, source_end: 9, source_unit: 'chars', ordinal: 0, url: '/v1/chunks/c1' },
  scores: { fused: 0.03, keyword_rank: 1, vector_rank: 1 }, ...over,
})
const meta = (over = {}) => ({ fusion_version: 'rrf-1', methods_used: ['keyword', 'vector'], degraded: null, latency_ms: 12, score_note: 'Scores are rank-based, not calibrated probabilities.', ...over })

beforeEach(() => {
  setToken('t'.repeat(20))
  window.location.hash = '#/search'
})
afterEach(() => {
  vi.restoreAllMocks()
  setToken(null)
})

async function ask(q = 'widget') {
  render(<App />)
  await userEvent.type(screen.getByLabelText(/search your documents/i), q)
  await userEvent.click(screen.getByRole('button', { name: 'Search' }))
}

it('renders document text as inert escaped text', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ results: [result()], meta: meta() }))
  await ask()
  expect(await screen.findByText(/window\.__pwned = 1/)).toBeInTheDocument()
  expect(document.querySelector('.excerpt script, .excerpt b')).toBeNull()
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  expect(screen.getByText(/page 2/)).toBeInTheDocument()
})

it('explains empty results and degraded mode', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    Response.json({ results: [], meta: meta({ degraded: 'embedding_unavailable', methods_used: ['keyword'] }) }),
  )
  await ask('nothing')
  expect(await screen.findByText(/No matching passages/)).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent(/keyword matches only/i)
})

it('shows a friendly rate-limit message', async () => {
  const env = { code: 'rate_limited', message: 'x', correlation_id: 'c', retryable: true }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json(env, { status: 429 }))
  await ask()
  expect(await screen.findByRole('alert')).toHaveTextContent(/too many searches/i)
})

it('opens a source reference', async () => {
  const chunk = { chunk_id: 'c1', document_id: 'd1', document_version_id: 'v1', document_name: 'markup.txt', media_type: 'text/plain',
    ordinal: 0, text: 'Full passage text', page_number: 2, section: null, source_start: 0, source_end: 9, metadata: {},
    is_current_version: true, original_url: '/v1/documents/d1/original', previous_chunk_id: null, next_chunk_id: 'c2' }
  vi.spyOn(globalThis, 'fetch').mockImplementation((u) =>
    Promise.resolve(String(u).includes('/v1/chunks/') ? Response.json(chunk) : Response.json({ results: [result()], meta: meta() })))
  await ask()
  await userEvent.click(await screen.findByRole('button', { name: 'Open source' }))
  expect(await screen.findByText('Full passage text')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /previous/i })).toBeDisabled()
  expect(screen.getByRole('button', { name: /next/i })).toBeEnabled()
})

it('sends signed-out users to the login form and rejects bad tokens', async () => {
  setToken(null)
  window.location.hash = '#/documents'
  const env = { code: 'unauthenticated', message: 'Authentication is required.', correlation_id: 'c', retryable: false }
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json(env, { status: 401 }))
  render(<App />)
  await userEvent.type(screen.getByLabelText(/owner access token/i), 'wrong-token-wrong-token')
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/not accepted/i)
})
