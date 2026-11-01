import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, TERMINAL, deleteDocument, listDocuments, type DocumentSummary } from '../api/client'
import { fileKind, relativeTime } from '../lib/format'
import { DevResetBanner } from './DevResetBanner'
import { DocumentDetailPanel } from './DocumentDetailPanel'
import { IconUpload } from './icons'
import { PipelineProgress } from './PipelineProgress'
import { Skeleton } from './Skeleton'
import { StatusBadge } from './StatusBadge'
import { UploadBox } from './UploadBox'

type Filter = 'all' | 'ready' | 'processing' | 'attention'
const group = (d: DocumentSummary): Exclude<Filter, 'all'> => {
  const s = d.latest_version.status
  return s === 'COMPLETED' ? 'ready' : s === 'FAILED' || s === 'UNSUPPORTED' ? 'attention' : 'processing'
}

export function DocumentsPage() {
  const [items, setItems] = useState<DocumentSummary[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [text, setText] = useState('')

  const remove = async (d: DocumentSummary) => {
    if (!window.confirm(`Permanently delete "${d.display_name}" and its indexed passages?`)) return
    try {
      await deleteDocument(d.id)
      if (selected === d.id) setSelected(null)
      await refresh()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not delete the document.')
    }
  }

  const refresh = useCallback(async () => {
    try {
      setItems((await listDocuments()).items)
      setErr(null)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Could not load documents.')
    }
  }, [])
  useEffect(() => {
    void refresh()
  }, [refresh])

  // Poll while anything is still processing; status is durable server-side, so refresh/leave is safe.
  const busy = items?.some((d) => !TERMINAL.includes(d.latest_version.status)) ?? false
  useEffect(() => {
    if (!busy) return
    const t = setInterval(() => void refresh(), 2000)
    return () => clearInterval(t)
  }, [busy, refresh])

  const counts = useMemo(() => {
    const c = { all: items?.length ?? 0, ready: 0, processing: 0, attention: 0 }
    items?.forEach((d) => c[group(d)]++)
    return c
  }, [items])

  const shown = useMemo(
    () =>
      (items ?? []).filter(
        (d) =>
          (filter === 'all' || group(d) === filter) &&
          d.display_name.toLowerCase().includes(text.trim().toLowerCase()),
      ),
    [items, filter, text],
  )

  return (
    <>
      <div className="page-head">
        <h1>Documents</h1>
        <p>Upload files and watch them move through the ingestion pipeline.</p>
      </div>
      {import.meta.env.DEV && <DevResetBanner onCleared={() => void refresh()} />}
      <UploadBox onUploaded={() => void refresh()} />

      <div className="stats" aria-label="Document summary">
        <div className="card stat"><b>{counts.all}</b><span>Total</span></div>
        <div className="card stat ok"><b>{counts.ready}</b><span>Ready to search</span></div>
        <div className="card stat busy"><b>{counts.processing}</b><span>Processing</span></div>
        <div className="card stat bad"><b>{counts.attention}</b><span>Needs attention</span></div>
      </div>

      {err && <p role="alert" className="notice bad">{err}</p>}
      {!items && !err && (
        <div className="card">
          <Skeleton lines={4} />
        </div>
      )}
      {items?.length === 0 && (
        <div className="card empty">
          <div className="empty-icon"><IconUpload size={28} /></div>
          <h2>No documents yet</h2>
          <p>Upload a text, Markdown, PDF, CSV or JSON file above to get started.</p>
        </div>
      )}
      {items && items.length > 0 && (
        <>
          <div className="toolbar">
            <div className="seg" role="group" aria-label="Filter by status">
              {(['all', 'ready', 'processing', 'attention'] as Filter[]).map((f) => (
                <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}>
                  {f === 'attention' ? 'Needs attention' : f[0].toUpperCase() + f.slice(1)} · {counts[f]}
                </button>
              ))}
            </div>
            <input type="text" aria-label="Filter documents by name" placeholder="Filter by name…" value={text} onChange={(e) => setText(e.target.value)} />
          </div>
          <div className="card table-wrap">
            <table>
              <caption className="visually-hidden">Uploaded documents</caption>
              <thead>
                <tr>
                  <th scope="col">Document</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="col-when">Uploaded</th>
                  <th scope="col"><span className="visually-hidden">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {shown.map((d) => {
                  const k = fileKind(d.display_name)
                  return (
                    <tr key={d.id}>
                      <th scope="row">
                        <div className="doc-cell">
                          <span className={`ftype kind-${k.kind}`}>{k.label}</span>
                          <button type="button" className="doc-name" onClick={() => setSelected(d.id)}>
                            {d.display_name}
                          </button>
                        </div>
                      </th>
                      <td>
                        <StatusBadge status={d.latest_version.status} />
                        <PipelineProgress status={d.latest_version.status} />
                        {d.latest_version.error_message && (
                          <div className="stage-line">{d.latest_version.error_message}</div>
                        )}
                      </td>
                      <td className="col-when muted small">{relativeTime(d.created_at)}</td>
                      <td>
                        <button type="button" aria-label={`Delete ${d.display_name}`} onClick={() => void remove(d)}>Delete</button>
                      </td>
                    </tr>
                  )
                })}
                {shown.length === 0 && (
                  <tr>
                    <td colSpan={4} className="muted" style={{ textAlign: 'center', padding: '1.5rem' }}>
                      No documents match this filter.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div aria-live="polite" className="visually-hidden">
        {busy ? 'Some documents are still processing.' : 'All documents are finished processing.'}
      </div>
      {selected && <DocumentDetailPanel id={selected} onClose={() => setSelected(null)} />}
    </>
  )
}
