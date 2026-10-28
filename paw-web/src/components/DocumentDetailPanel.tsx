import { useEffect, useState } from 'react'
import { ApiError, TERMINAL, downloadOriginal, getDocument, type DocumentDetail } from '../api/client'
import { StatusBadge } from './StatusBadge'

export function DocumentDetailPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const d = await getDocument(id)
        if (stop) return
        setDoc(d)
        if (!TERMINAL.includes(d.versions[0].status)) timer = setTimeout(load, 2000)
      } catch (e) {
        if (!stop) setErr(e instanceof ApiError ? e.message : 'Could not load the document.')
      }
    }
    void load()
    return () => {
      stop = true
      clearTimeout(timer)
    }
  }, [id])

  const v = doc?.versions[0]
  return (
    <aside className="card detail" aria-label="Document details">
      <div className="row">
        <h2>{doc?.display_name ?? 'Document'}</h2>
        <button type="button" className="link-btn" onClick={onClose}>
          Close
        </button>
      </div>
      {err && <p role="alert" className="error-text">{err}</p>}
      {doc && v && (
        <>
          <p>
            <StatusBadge status={v.status} /> · {v.chunk_count} passages · v{v.version_no} ·{' '}
            {(v.size_bytes / 1024).toFixed(1)} KiB
          </p>
          {v.error_message && (
            <p role="alert" className="error-text">
              {v.error_message} <span className="muted">({v.error_code})</span>
            </p>
          )}
          <h3>Processing timeline</h3>
          <ol className="timeline">
            {v.events.map((e, i) => (
              <li key={i}>
                <strong>{e.status}</strong> <time dateTime={e.created_at}>{new Date(e.created_at).toLocaleTimeString()}</time>
                {e.detail && <div className="muted">{e.detail}</div>}
              </li>
            ))}
          </ol>
          <button
            type="button"
            onClick={() =>
              void downloadOriginal(`/v1/documents/${doc.id}/original?version_id=${v.id}`, doc.display_name)
            }
          >
            Download original
          </button>
          <p className="muted">sha256 {v.sha256.slice(0, 16)}… · embedding {v.embedding_model ?? '—'}</p>
        </>
      )}
    </aside>
  )
}
