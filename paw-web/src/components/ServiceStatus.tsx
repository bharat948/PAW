import { useReadiness } from '../hooks/useReadiness'
import { ErrorState } from './ErrorState'
import { Skeleton } from './Skeleton'

export function ServiceStatus() {
  const { state, refresh } = useReadiness()

  if (state.phase === 'loading')
    return (
      <div role="status" aria-label="Checking service status">
        <h1>Service status</h1>
        <p className="visually-hidden">Checking service status…</p>
        <div className="card"><Skeleton lines={2} /></div>
      </div>
    )

  if (state.phase === 'ready') {
    return (
      <section aria-labelledby="status-h">
        <h1 id="status-h">Service status</h1>
        <div className="card stack" role="status">
          <div><span className="pill ok">Backend: ready</span></div>
          <div><span className="pill ok">Database: ready</span></div>
        </div>
      </section>
    )
  }

  const dbProblem = state.failedCheck === 'database' || state.failedCheck === 'migrations'
  return (
    <ErrorState title="Service unavailable" onRetry={refresh}>
      <p>
        {state.unreachable
          ? 'The PAW backend could not be reached.'
          : dbProblem
            ? 'The backend is running, but its database is not ready.'
            : 'The backend is not ready yet.'}{' '}
        Please try again shortly.
      </p>
      {state.correlationId && <p className="muted">Reference: {state.correlationId}</p>}
    </ErrorState>
  )
}
