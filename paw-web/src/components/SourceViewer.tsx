import { useEffect, useState } from 'react'
import { ApiError, downloadOriginal, getChunk, type ChunkDetail } from '../api/client'
import { fileKind } from '../lib/format'
import { Highlight } from './Highlight'
import { IconArrowLeft, IconArrowRight, IconDownload, IconX } from './icons'
import { Skeleton } from './Skeleton'

export function describeSource(c: {
  page_number: number | null
  section: string | null
  source_start: number | null
  source_end: number | null
  metadata?: Record<string, unknown>
  source_unit?: 'chars' | 'rows'
}): string {
  const unit = c.source_unit ?? (c.metadata?.source_unit as string | undefined)
  const parts: string[] = []
  if (c.page_number) parts.push(`page ${c.page_number}`)
  if (c.section) parts.push(`section "${c.section}"`)
  if (unit === 'rows')
    parts.push(c.source_start === c.source_end ? `row ${c.source_start}` : `rows ${c.source_start}–${c.source_end}`)
  else if (c.source_start !== null && !c.page_number) parts.push(`characters ${c.source_start}–${c.source_end}`)
  return parts.join(' · ') || 'document'
}

type Rows = Record<string, unknown>[]

export function SourceViewer({ chunkId, terms, onClose }: { chunkId: string; terms: string[]; onClose: () => void }) {
  const [id, setId] = useState(chunkId)
  const [loaded, setLoaded] = useState<ChunkDetail | null>(null)
  const [failed, setFailed] = useState<{ id: string; message: string } | null>(null)

  useEffect(() => {
    let stale = false
    getChunk(id)
      .then((c) => !stale && setLoaded(c))
      .catch((e: unknown) =>
        !stale && setFailed({ id, message: e instanceof ApiError ? e.message : 'Could not load the source.' }),
      )
    return () => {
      stale = true
    }
  }, [id])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Derived, so switching passages never needs a synchronous setState in an effect.
  const chunk = loaded && loaded.chunk_id === id ? loaded : null
  const err = failed && failed.id === id ? failed.message : null
  const rows = chunk && Array.isArray(chunk.metadata.rows) ? (chunk.metadata.rows as Rows) : null
  const columns = rows ? Array.from(new Set(rows.flatMap((r) => Object.keys(r)))) : []
  const k = fileKind(chunk?.document_name ?? '')

  return (
    <>
    <div className="sheet-backdrop" onClick={onClose} aria-hidden="true" />
    <aside className="card source" aria-label="Source passage">
      <div className="source-head">
        {chunk && <span className={`ftype kind-${k.kind}`} style={{ width: 32, height: 32, fontSize: '.58rem' }}>{k.label}</span>}
        <h2 style={{ flex: 1, overflowWrap: 'anywhere', fontSize: '1rem' }}>{chunk?.document_name ?? 'Source'}</h2>
        <button type="button" className="ghost icon-btn" onClick={onClose} aria-label="Close source">
          <IconX />
        </button>
      </div>
      <div className="source-body">
        {err && <p role="alert" className="error-text">{err}</p>}
        {!chunk && !err && <Skeleton lines={5} />}
        {chunk && (
          <>
            <div className="result-sub">
              <span className="tag">{describeSource({ ...chunk })}</span>
              <span className="tag dim">passage {chunk.ordinal + 1}</span>
              {!chunk.is_current_version && <span className="pill warn">older version</span>}
            </div>
            {rows ? (
              <div style={{ overflowX: 'auto', margin: '.75rem 0' }}>
                <table className="mini-table">
                  <thead>
                    <tr>{columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr key={i}>
                        {columns.map((c) => (
                          <td key={c}><Highlight text={String(r[c] ?? '')} terms={terms} /></td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              /* Rendered as text, never HTML: stored documents are untrusted input. */
              <pre className="passage"><Highlight text={chunk.text} terms={terms} /></pre>
            )}
            {rows && <pre className="visually-hidden">{chunk.text}</pre>}
            <div className="pager">
              <button type="button" disabled={!chunk.previous_chunk_id} onClick={() => chunk.previous_chunk_id && setId(chunk.previous_chunk_id)}>
                <IconArrowLeft size={16} /> Previous passage
              </button>
              <button type="button" disabled={!chunk.next_chunk_id} onClick={() => chunk.next_chunk_id && setId(chunk.next_chunk_id)}>
                Next passage <IconArrowRight size={16} />
              </button>
            </div>
            <p style={{ marginTop: '.85rem' }}>
              <button type="button" className="ghost" onClick={() => void downloadOriginal(chunk.original_url, chunk.document_name)}>
                <IconDownload size={16} /> Download original
              </button>
            </p>
          </>
        )}
      </div>
    </aside>
    </>
  )
}
