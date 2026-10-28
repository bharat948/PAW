import { useCallback, useEffect, useState } from 'react'
import { ApiError, TERMINAL, listDocuments, type DocumentSummary } from '../api/client'
import { DocumentDetailPanel } from './DocumentDetailPanel'
import { Loading } from './Loading'
import { StatusBadge } from './StatusBadge'
import { UploadBox } from './UploadBox'

export function DocumentsPage() {
  const [items, setItems] = useState<DocumentSummary[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

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

  return (
    <>
      <h1>Documents</h1>
      <UploadBox onUploaded={() => void refresh()} />
      {err && (
        <p role="alert" className="error-text">
          {err}
        </p>
      )}
      {!items && !err && <Loading label="Loading documents…" />}
      {items?.length === 0 && <p className="muted">No documents yet. Upload one to get started.</p>}
      {items && items.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Uploaded documents</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Status</th>
                <th scope="col">Uploaded</th>
              </tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr key={d.id}>
                  <th scope="row">
                    <button type="button" className="link-btn" onClick={() => setSelected(d.id)}>
                      {d.display_name}
                    </button>
                  </th>
                  <td>
                    <StatusBadge status={d.latest_version.status} />
                    {d.latest_version.error_message && (
                      <div className="muted">{d.latest_version.error_message}</div>
                    )}
                  </td>
                  <td>{new Date(d.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div aria-live="polite" className="visually-hidden">
        {busy ? 'Some documents are still processing.' : 'All documents are finished processing.'}
      </div>
      {selected && <DocumentDetailPanel id={selected} onClose={() => setSelected(null)} />}
    </>
  )
}
