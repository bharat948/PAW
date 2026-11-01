import type { RetrievalEvent } from '../api/client'
import { queryTerms } from '../lib/format'
import { Highlight } from './Highlight'

/** Shows exactly what retrieval did for an answer, so the answer can be verified against the database. */
export function RetrievalTrace({
  r, cited, active, onOpen,
}: { r: RetrievalEvent; cited: number[]; active: number | null; onOpen: (n: number) => void }) {
  const terms = queryTerms(r.query_used)
  const m = r.meta
  return (
    <details className="trace">
      <summary>
        Retrieval trace · {r.sources.length} source{r.sources.length === 1 ? '' : 's'} · {m.latency_ms} ms
      </summary>
      <div className="trace-body">
        <div>
          <span className="muted small">Search query used{r.rewritten ? ' (rewritten from your follow-up)' : ''}</span>
          <div><strong>{r.query_used}</strong></div>
        </div>
        <div className="trace-kv">
          <span className="tag">{m.methods_used.join(' + ')}</span>
          <span className="tag dim">keyword candidates {m.candidates.keyword}</span>
          <span className="tag dim">vector candidates {m.candidates.vector}</span>
          <span className="tag dim">embeddings: {m.embedding_model}{m.semantic ? '' : ' (demo, no meaning)'}</span>
          <span className="tag dim">model: {m.llm_model}</span>
          {m.degraded && <span className="pill warn">{m.degraded}</span>}
        </div>
        {r.sources.map((s) => (
          <div key={s.n} className={`src${active === s.n ? ' on' : ''}`}>
            <div className="head">
              <span className="num">{s.n}</span>
              <strong>{s.document_name}</strong>
              <span className="tag">{s.label.replace(s.document_name + ', ', '') || 'document'}</span>
              <span className={`tag ${s.method === 'hybrid' ? 'hybrid' : ''}`}>{s.method}</span>
              {cited.includes(s.n) ? <span className="pill ok">cited</span> : <span className="tag dim">not cited</span>}
              <button type="button" className="link-btn" style={{ marginLeft: 'auto' }} onClick={() => onOpen(s.n)}>
                Open source
              </button>
            </div>
            <p><Highlight text={s.excerpt} terms={terms} /></p>
          </div>
        ))}
        {r.sources.length === 0 && <p className="muted small">Nothing relevant was found, so the model was not called.</p>}
      </div>
    </details>
  )
}
