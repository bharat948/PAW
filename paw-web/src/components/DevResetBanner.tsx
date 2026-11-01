import { useState } from 'react'
import { ApiError, clearAllData } from '../api/client'

export function DevResetBanner({ onCleared }: { onCleared: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const clear = async () => {
    setBusy(true)
    try {
      const r = await clearAllData()
      setMsg({ ok: true, text: `Cleared ${r.documents_deleted} document(s).` })
      onCleared()
    } catch (e) {
      setMsg({ ok: false, text: e instanceof ApiError ? e.message : 'Could not clear data.' })
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <div className="notice" role="region" aria-label="Development tools">
      <div style={{ flex: 1 }}>
        <strong>Dev mode:</strong> you can wipe every uploaded document, chunk and search log from the
        database to start fresh with your own files. This cannot be undone.
        {msg && (
          <div role="status" className={msg.ok ? 'ok-text' : 'error-text'}>
            {msg.text}
          </div>
        )}
      </div>
      {confirming ? (
        <span style={{ display: 'flex', gap: '.5rem' }}>
          <button type="button" disabled={busy} onClick={() => void clear()}>
            {busy ? 'Clearing…' : 'Yes, delete everything'}
          </button>
          <button type="button" disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </span>
      ) : (
        <button type="button" onClick={() => setConfirming(true)}>
          Clear all data
        </button>
      )}
    </div>
  )
}
