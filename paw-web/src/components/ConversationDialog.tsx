import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'

export function ConversationDialog({ title, initial = '', inputLabel, children, confirm, onConfirm, onClose }: {
  title: string; initial?: string; inputLabel?: string; children?: ReactNode; confirm: string
  onConfirm: (value: string, includeDelegated: boolean) => Promise<void>; onClose: () => void
}) {
  const [value, setValue] = useState(initial)
  const [include, setInclude] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const panel = useRef<HTMLFormElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    panel.current?.querySelector<HTMLElement>('textarea, input, button')?.focus()
    return () => previous?.focus()
  }, [])
  const key = (e: KeyboardEvent) => {
    if (e.key === 'Escape' && !busy) onClose()
    if (e.key !== 'Tab') return
    const nodes = [...(panel.current?.querySelectorAll<HTMLElement>('input:not(:disabled),textarea:not(:disabled),button:not(:disabled)') ?? [])]
    const first = nodes[0], last = nodes.at(-1)
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
    if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true); setError('')
    try { await onConfirm(value, include); onClose() }
    catch { setError('Could not save this change. Please try again.') }
    finally { setBusy(false) }
  }
  return <div className="conversation-modal">
    <form ref={panel} className="card" role="dialog" aria-modal="true" aria-labelledby="conversation-dialog-title" onKeyDown={key} onSubmit={(e) => void submit(e)}>
      <h2 id="conversation-dialog-title">{title}</h2>
      {children}
      {inputLabel && <label>{inputLabel}<textarea maxLength={inputLabel === 'Conversation title' ? 200 : 8000} value={value} onChange={(e) => setValue(e.target.value)} required disabled={busy} /></label>}
      {confirm === 'Delete conversation' && <label><input type="checkbox" checked={include} onChange={(e) => setInclude(e.target.checked)} disabled={busy} /> Also delete delegated conversations</label>}
      {error && <p role="alert">{error}</p>}
      <div className="conversation-actions"><button type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="primary" disabled={busy || (!!inputLabel && !value.trim())}>{busy ? 'Saving…' : confirm}</button></div>
    </form>
  </div>
}
