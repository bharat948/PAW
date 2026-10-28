import { useState, type FormEvent } from 'react'
import { ApiError, search, type SearchResponse } from '../api/client'
import { Loading } from './Loading'
import { SourceViewer, describeSource } from './SourceViewer'

const DEGRADED: Record<string, string> = {
  embedding_unavailable: 'Semantic search is unavailable right now; showing keyword matches only.',
  embedding_budget_exceeded: 'The daily embedding budget is used up; showing keyword matches only.',
}

export function SearchPage() {
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(10)
  const [state, setState] = useState<
    | { phase: 'idle' }
    | { phase: 'loading' }
    | { phase: 'done'; asked: string; data: SearchResponse }
    | { phase: 'error'; message: string }
  >({ phase: 'idle' })
  const [openChunk, setOpenChunk] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const q = query.trim()
    if (!q) return
    setState({ phase: 'loading' })
    setOpenChunk(null)
    try {
      setState({ phase: 'done', asked: q, data: await search(q, topK) })
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.status === 429
            ? 'Too many searches. Please wait a moment and try again.'
            : err.status === 0
              ? 'The PAW service could not be reached.'
              : err.message
          : 'Search failed.'
      setState({ phase: 'error', message })
    }
  }

  return (
    <>
      <h1>Search</h1>
      <form onSubmit={submit} role="search" className="card search-form">
        <label htmlFor="q">Search your documents</label>
        <input id="q" type="search" value={query} maxLength={1000} onChange={(e) => setQuery(e.target.value)} />
        <label htmlFor="k">Results</label>
        <select id="k" value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
          {[5, 10, 20, 50].map((n) => (
            <option key={n}>{n}</option>
          ))}
        </select>
        <button type="submit" disabled={!query.trim() || state.phase === 'loading'}>
          Search
        </button>
      </form>

      <div aria-live="polite">
        {state.phase === 'loading' && <Loading label="Searching…" />}
        {state.phase === 'error' && (
          <p role="alert" className="error-text">
            {state.message}
          </p>
        )}
        {state.phase === 'done' && (
          <>
            {state.data.meta.degraded && (
              <p className="notice" role="status">
                {DEGRADED[state.data.meta.degraded] ?? 'Some search features are unavailable.'}
              </p>
            )}
            <p className="muted">
              {state.data.results.length} result{state.data.results.length === 1 ? '' : 's'} for “{state.asked}” ·{' '}
              {state.data.meta.methods_used.join(' + ')} · {state.data.meta.latency_ms} ms
            </p>
            {state.data.results.length === 0 && (
              <p>No matching passages. Try different words, or upload a document that covers this topic.</p>
            )}
            <ol className="results">
              {state.data.results.map((r) => (
                <li key={r.chunk_id} className="card">
                  <div className="row">
                    <strong>{r.document_name}</strong>
                    <span className="muted">{describeSource({ ...r.source })}</span>
                    <span className={`badge ${r.retrieval_method === 'hybrid' ? 'ok' : 'busy'}`}>{r.retrieval_method}</span>
                  </div>
                  {/* React escapes this string; stored text is never injected as HTML. */}
                  <p className="excerpt">
                    {r.excerpt}
                    {r.excerpt_truncated && '…'}
                  </p>
                  <button type="button" className="link-btn" onClick={() => setOpenChunk(r.chunk_id)}>
                    Open source
                  </button>
                </li>
              ))}
            </ol>
            <p className="muted">{state.data.meta.score_note}</p>
          </>
        )}
      </div>
      {openChunk && <SourceViewer chunkId={openChunk} onClose={() => setOpenChunk(null)} />}
    </>
  )
}
