import { useEffect, useRef, useState } from 'react'
import {
  ApiError, TERMINAL, downloadOriginal, getDocument, listChunks,
  type ChunkOut, type DocumentDetail,
} from '../api/client'
import { fileKind, formatBytes } from '../lib/format'
import { IconDownload, IconX } from './icons'
import { PipelineProgress } from './PipelineProgress'
import { Skeleton } from './Skeleton'
import { StatusBadge } from './StatusBadge'

type Tab = 'overview' | 'timeline' | 'passages'

export function DocumentDetailPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [chunks, setChunks] = useState<ChunkOut[] | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

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

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const v = doc?.versions[0]
  const done = v?.status === 'COMPLETED'
  useEffect(() => {
    if (tab !== 'passages' || !doc || !v || chunks) return
    listChunks(doc.id, v.id)
      .then((r) => setChunks(r.items))
      .catch(() => setChunks([]))
  }, [tab, doc, v, chunks])

  const k = fileKind(doc?.display_name ?? '')
  return (
    <>
      <div className="backdrop" onClick={onClose} aria-hidden="true" />
      <div className="drawer" role="dialog" aria-modal="true" aria-label="Document details">
        <div className="drawer-head">
          <span className={`ftype kind-${k.kind}`}>{k.label}</span>
          <h2>{doc?.display_name ?? 'Document'}</h2>
          <button ref={closeRef} type="button" className="ghost icon-btn" onClick={onClose} aria-label="Close details">
            <IconX />
          </button>
        </div>
        <div className="tabs" role="tablist" aria-label="Document sections">
          {(['overview', 'timeline', 'passages'] as Tab[]).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        <div className="drawer-body">
          {err && <p role="alert" className="error-text">{err}</p>}
          {!doc && !err && <Skeleton lines={5} />}
          {doc && v && tab === 'overview' && (
            <>
              <p style={{ display: 'flex', gap: '.6rem', alignItems: 'center', flexWrap: 'wrap' }}>
                <StatusBadge status={v.status} />
                <span className="muted small">version {v.version_no}</span>
              </p>
              <PipelineProgress status={v.status} />
              {v.error_message && (
                <p role="alert" className={v.status === 'UNSUPPORTED' ? 'notice' : 'notice bad'} style={{ marginTop: '.75rem' }}>
                  {v.error_message}
                </p>
              )}
              <div className="kv" style={{ marginTop: '1rem' }}>
                <div><b>{v.chunk_count}</b><span>Searchable passages</span></div>
                <div><b>{formatBytes(v.size_bytes)}</b><span>Original size</span></div>
                <div><b>{v.attempt_count ?? 0}</b><span>Processing attempts</span></div>
                <div><b className="mono">{v.embedding_model ?? '—'}</b><span>Embedding model</span></div>
                <div style={{ gridColumn: '1 / -1' }}><b className="mono">{v.sha256}</b><span>SHA-256 of the stored original</span></div>
              </div>
              <p style={{ marginTop: '1rem' }}>
                <button
                  type="button"
                  onClick={() => void downloadOriginal(`/v1/documents/${doc.id}/original?version_id=${v.id}`, doc.display_name)}
                >
                  <IconDownload size={16} /> Download original
                </button>
              </p>
            </>
          )}
          {doc && v && tab === 'timeline' && (
            <>
              <h3 style={{ marginTop: 0 }}>Processing timeline</h3>
              <ol className="timeline">
                {v.events.map((e, i) => (
                  <li key={i} className={e.status}>
                    <strong>{e.status}</strong>
                    <time dateTime={e.created_at}>{new Date(e.created_at).toLocaleTimeString()}</time>
                    {e.detail && <div className="muted small">{e.detail}</div>}
                  </li>
                ))}
              </ol>
            </>
          )}
          {doc && v && tab === 'passages' && (
            <>
              {!done && <p className="muted">Passages appear once processing completes.</p>}
              {done && !chunks && <Skeleton lines={6} />}
              {chunks?.map((c) => (
                <article key={c.id} className="passage-card">
                  <div className="result-sub">
                    <span className="tag">#{c.ordinal + 1}</span>
                    {c.page_number && <span className="tag">Page {c.page_number}</span>}
                    {c.section && <span className="tag">{c.section}</span>}
                    {c.metadata.source_unit === 'rows' && (
                      <span className="tag">Rows {c.source_start}–{c.source_end}</span>
                    )}
                  </div>
                  <p>{c.text.length > 400 ? `${c.text.slice(0, 400)}…` : c.text}</p>
                </article>
              ))}
              {chunks && chunks.length === 0 && done && <p className="muted">No passages to show.</p>}
              {chunks && v.chunk_count > chunks.length && (
                <p className="muted small">Showing the first {chunks.length} of {v.chunk_count} passages.</p>
              )}
            </>
          )}
        </div>
      </div>
    </>
  )
}
