import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  ApiError, listDocuments, search, type DocumentSummary, type SearchResponse, type SearchResult,
} from '../api/client'
import { fileKind } from '../lib/format'
import { Highlight } from './Highlight'
import { IconAlert, IconInfo, IconSearch, IconSliders, IconSpark } from './icons'
import { Skeleton } from './Skeleton'
import { SourceViewer, describeSource } from './SourceViewer'
import { queryTerms } from '../lib/format'

const DEGRADED: Record<string, string> = {
  embedding_unavailable: 'Semantic search is unavailable right now; showing keyword matches only.',
  embedding_budget_exceeded: 'The daily embedding budget is used up; showing keyword matches only.',
}
const METHOD_HINT: Record<string, string> = {
  hybrid: 'Matched by both keywords and meaning',
  keyword: 'Matched by exact words',
  vector: 'Matched by meaning',
}
const RECENT_KEY = 'paw.recent'

const loadRecent = (): string[] => {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]') as string[]
  } catch {
    return []
  }
}
const saveRecent = (q: string, prev: string[]): string[] => {
  const next = [q, ...prev.filter((x) => x !== q)].slice(0, 6)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    /* storage unavailable: history is a convenience only */
  }
  return next
}

type State =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'done'; asked: string; data: SearchResponse }
  | { phase: 'error'; message: string }

export function SearchPage() {
  const input = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(10)
  const [state, setState] = useState<State>({ phase: 'idle' })
  const [openChunk, setOpenChunk] = useState<string | null>(null)
  const [recent, setRecent] = useState<string[]>(loadRecent)
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null)
  const [scope, setScope] = useState<string[]>([])

  // "/" focuses the search box, like most search UIs
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) {
        e.preventDefault()
        input.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  function loadDocs() {
    if (docs) return
    listDocuments()
      .then((r) => setDocs((r.items ?? []).filter((d) => d.latest_version.status === 'COMPLETED')))
      .catch(() => setDocs([]))
  }

  async function run(q: string) {
    q = q.trim()
    if (!q) return
    setQuery(q)
    setState({ phase: 'loading' })
    setOpenChunk(null)
    try {
      const data = await search(q, topK, scope)
      setState({ phase: 'done', asked: q, data })
      setRecent((r) => saveRecent(q, r))
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
  const submit = (e: FormEvent) => {
    e.preventDefault()
    void run(query)
  }

  const asked = state.phase === 'done' ? state.asked : ''
  const terms = useMemo(() => queryTerms(asked), [asked])
  const top = state.phase === 'done' ? (state.data.results[0]?.scores.fused ?? 1) : 1

  return (
    <>
      <div className="page-head">
        <h1>Search</h1>
        <p>Ask in your own words. Results combine exact keywords with meaning, and link back to the exact source.</p>
      </div>

      <form onSubmit={submit} role="search" className="card search-hero">
        <label htmlFor="q" className="visually-hidden">Search your documents</label>
        <div className="searchbox">
          <div className="field">
            <span className="si"><IconSearch size={20} /></span>
            <input
              id="q" ref={input} type="search" value={query} maxLength={1000} autoComplete="off"
              placeholder="Search your documents…" onChange={(e) => setQuery(e.target.value)}
            />
            <kbd aria-hidden="true">/</kbd>
          </div>
          <button type="submit" className="primary" disabled={!query.trim() || state.phase === 'loading'}>
            Search
          </button>
        </div>

        <details className="opts" onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && loadDocs()}>
          <summary><IconSliders size={16} /> Options{scope.length > 0 && ` · ${scope.length} document${scope.length > 1 ? 's' : ''} selected`}</summary>
          <div className="opts-body">
            <div>
              <label htmlFor="k">Results</label>
              <select id="k" value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
                {[5, 10, 20, 50].map((n) => <option key={n}>{n}</option>)}
              </select>
            </div>
            <div>
              <span style={{ fontWeight: 600, fontSize: '.88rem', display: 'block', marginBottom: '.25rem' }}>Limit to documents</span>
              {!docs && <Skeleton lines={1} />}
              {docs?.length === 0 && <span className="muted small">No searchable documents yet.</span>}
              <div className="scope">
                {docs?.map((d) => (
                  <label key={d.id}>
                    <input
                      type="checkbox" className="visually-hidden-input" checked={scope.includes(d.id)}
                      onChange={(e) => setScope((s) => (e.target.checked ? [...s, d.id] : s.filter((x) => x !== d.id)))}
                    />
                    {d.display_name}
                  </label>
                ))}
              </div>
            </div>
          </div>
        </details>

        {state.phase === 'idle' && recent.length > 0 && (
          <div className="recent">
            <span className="muted small">Recent</span>
            {recent.map((r) => (
              <button key={r} type="button" onClick={() => void run(r)}>{r}</button>
            ))}
          </div>
        )}
      </form>

      <div className={`search-layout${openChunk ? ' with-source' : ''}`} aria-live="polite">
        <div>
          {state.phase === 'idle' && (
            <div className="card empty">
              <div className="empty-icon"><IconSpark size={28} /></div>
              <h2>Find anything in your documents</h2>
              <p>Try a name, a number, or a question. Press <kbd>/</kbd> to jump to the search box.</p>
            </div>
          )}
          {state.phase === 'loading' && (
            <ul className="results" aria-label="Loading results" role="status">
              {[0, 1, 2].map((i) => (
                <li key={i} className="card"><Skeleton lines={4} /></li>
              ))}
            </ul>
          )}
          {state.phase === 'error' && (
            <p role="alert" className="notice bad"><IconAlert size={18} /> {state.message}</p>
          )}
          {state.phase === 'done' && (
            <>
              {state.data.meta.degraded && (
                <p className="notice" role="status">
                  <IconAlert size={18} />
                  {DEGRADED[state.data.meta.degraded] ?? 'Some search features are unavailable.'}
                </p>
              )}
              {state.data.meta.semantic === false && (
                <p className="notice info">
                  <IconInfo size={18} />
                  Meaning-based matching is off: this server uses demo embeddings, so only keyword matches are
                  meaningful. Configure an embedding provider for true semantic search.
                </p>
              )}
              <div className="result-meta">
                <strong style={{ color: 'var(--fg)' }}>
                  {state.data.results.length} result{state.data.results.length === 1 ? '' : 's'}
                </strong>
                <span>for “{state.asked}”</span>
                <span className="tag dim">{state.data.meta.methods_used.join(' + ')}</span>
                <span className="tag dim">{state.data.meta.latency_ms} ms</span>
              </div>
              {state.data.results.length === 0 && (
                <div className="card empty">
                  <div className="empty-icon"><IconSearch size={28} /></div>
                  <h2>No matching passages</h2>
                  <p>Try different words, or upload a document that covers this topic.</p>
                </div>
              )}
              <ol className="results">
                {state.data.results.map((r, i) => (
                  <ResultCard
                    key={r.chunk_id} r={r} i={i} terms={terms} top={top}
                    active={openChunk === r.chunk_id} onOpen={() => setOpenChunk(r.chunk_id)}
                  />
                ))}
              </ol>
              <p className="muted small" style={{ marginTop: '.75rem' }}>{state.data.meta.score_note}</p>
            </>
          )}
        </div>
        {openChunk && <SourceViewer key={openChunk} chunkId={openChunk} terms={terms} onClose={() => setOpenChunk(null)} />}
      </div>
    </>
  )
}

function ResultCard({ r, i, terms, top, active, onOpen }: {
  r: SearchResult; i: number; terms: string[]; top: number; active: boolean; onOpen: () => void
}) {
  const k = fileKind(r.document_name)
  const rel = top > 0 ? Math.max(6, Math.round((r.scores.fused / top) * 100)) : 100
  return (
    <li className={`card result${active ? ' active' : ''}`} style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}>
      <div className="result-head">
        <span className={`ftype kind-${k.kind}`}>{k.label}</span>
        <div style={{ minWidth: 0 }}>
          <div className="result-title">{r.document_name}</div>
          <div className="result-sub">
            <span className="tag">{describeSource({ ...r.source })}</span>
            <span className={`tag ${r.retrieval_method === 'hybrid' ? 'hybrid' : ''}`} title={METHOD_HINT[r.retrieval_method]}>
              {r.retrieval_method}
            </span>
          </div>
        </div>
        <div className="rel" title="Relative rank versus the top result (not a probability)">
          <div className="bar" role="img" aria-label={`Relative rank ${rel}% of the top result`}><i style={{ width: `${rel}%` }} /></div>
        </div>
      </div>
      {/* React escapes this string; stored text is never injected as HTML. */}
      <p className="excerpt">
        {(r.excerpt_start ?? 0) > 0 && '…'}
        <Highlight text={r.excerpt} terms={terms} />
        {r.excerpt_truncated && '…'}
      </p>
      <div className="result-foot">
        <button type="button" onClick={onOpen}>Open source</button>
      </div>
    </li>
  )
}
