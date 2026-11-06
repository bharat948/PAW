import { useCallback, useEffect, useState } from 'react'
import { ApiError, decideApproval, listApprovals, reconcileApproval, stepUpUrl, type Approval } from '../api/client'

export function ApprovalInbox() {
  const [items, setItems] = useState<Approval[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [stepup, setStepup] = useState(false)
  const load = useCallback(async () => {
    try { setItems((await listApprovals()).approvals) }
    catch { setError('Could not load approvals. Try refreshing.') }
  }, [])
  useEffect(() => {
    let alive = true
    listApprovals().then(r => { if (alive) setItems(r.approvals) }).catch(() => { if (alive) setError('Could not load approvals. Try refreshing.') })
    return () => { alive = false }
  }, [])
  async function act(item: Approval, action: 'approve' | 'reject' | 'check') {
    setBusy(item.id)
    setError(null)
    setStepup(false)
    try {
      if (action === 'check') await reconcileApproval(item.id)
      else await decideApproval(item, action === 'approve')
      await load()
    } catch (e) {
      setStepup(e instanceof ApiError && e.envelope?.code === 'stepup_required')
      setError(e instanceof ApiError ? e.message : 'The action could not be completed. Refresh to check its status.')
    } finally { setBusy(null) }
  }
  return <section aria-labelledby="approval-heading">
    <h2 id="approval-heading">Issue approvals</h2>
    <p className="muted">Review the exact repository, title and body before allowing PAW to create an issue. Each approval can be used once.</p>
    <button disabled={busy !== null} onClick={load}>Refresh approvals</button>
    {error && <p role="alert" className="error-text">{error}</p>}
    {stepup && <a className="btn primary" href={stepUpUrl('/#/integrations')}>Sign in again to confirm</a>}
    {!items && !error && <p role="status">Loading approvals…</p>}
    {items?.length === 0 && <p>No issue approvals yet.</p>}
    {items?.map(item => <article className="card" key={item.id} aria-label={`Issue approval: ${item.args.title}`}>
      <p><strong>{item.repository}</strong> · {item.operation_state ?? item.state}</p>
      <h3>{item.args.title}</h3>
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.args.body || '(Empty body)'}</pre>
      <p className="muted">Requested by {item.actor_id}. Expires {new Date(item.expires_at).toLocaleString()}. PAW adds an operation marker to the issue for recovery.</p>
      {item.state === 'PENDING' && <div className="actions">
        <button className="primary" disabled={busy !== null} onClick={() => act(item, 'approve')}>Approve and create issue</button>
        <button disabled={busy !== null} onClick={() => act(item, 'reject')}>Reject</button>
      </div>}
      {(item.operation_state === 'UNKNOWN' || item.operation_state === 'STARTED') && <>
        <p role="status">GitHub may have created this issue. Checking searches for the existing issue and does not create another.</p>
        <button disabled={busy !== null} onClick={() => act(item, 'check')}>Check GitHub outcome</button>
      </>}
      {item.result?.url && /^https:\/\/github\.com\//.test(item.result.url) && <a href={item.result.url} target="_blank" rel="noreferrer">Open created issue</a>}
    </article>)}
  </section>
}
