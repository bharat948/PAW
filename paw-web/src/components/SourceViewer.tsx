import { useEffect, useState } from 'react'
import { ApiError, downloadOriginal, getChunk, type ChunkDetail } from '../api/client'
import { Loading } from './Loading'

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
  if (unit === 'rows') parts.push(c.source_start === c.source_end ? `row ${c.source_start}` : `rows ${c.source_start}–${c.source_end}`)
  else if (c.source_start !== null && !c.page_number) parts.push(`characters ${c.source_start}–${c.source_end}`)
  return parts.join(' · ') || 'document'
}

export function SourceViewer({ chunkId, onClose }: { chunkId: string; onClose: () => void }) {
  const [id, setId] = useState(chunkId)
  const [chunk, setChunk] = useState<ChunkDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setChunk(null)
    setErr(null)
    getChunk(id)
      .then(setChunk)
      .catch((e: unknown) => setErr(e instanceof ApiError ? e.message : 'Could not load the source.'))
  }, [id])

  return (
    <aside className="card detail" aria-label="Source passage">
      <div className="row">
        <h2>Source</h2>
        <button type="button" className="link-btn" onClick={onClose}>
          Close
        </button>
      </div>
      {err && <p role="alert" className="error-text">{err}</p>}
      {!chunk && !err && <Loading label="Loading source…" />}
      {chunk && (
        <>
          <p>
            <strong>{chunk.document_name}</strong> — {describeSource({ ...chunk })}
            {!chunk.is_current_version && <span className="badge busy">older version</span>}
          </p>
          {/* Rendered as text, never HTML: stored documents are untrusted input. */}
          <pre className="passage">{chunk.text}</pre>
          <div className="row">
            <button type="button" disabled={!chunk.previous_chunk_id} onClick={() => chunk.previous_chunk_id && setId(chunk.previous_chunk_id)}>
              ← Previous passage
            </button>
            <button type="button" disabled={!chunk.next_chunk_id} onClick={() => chunk.next_chunk_id && setId(chunk.next_chunk_id)}>
              Next passage →
            </button>
            <button type="button" onClick={() => void downloadOriginal(chunk.original_url, chunk.document_name)}>
              Download original
            </button>
          </div>
        </>
      )}
    </aside>
  )
}
