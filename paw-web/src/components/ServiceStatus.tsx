import { useReadiness } from '../hooks/useReadiness'
import { ErrorState } from './ErrorState'
import { Loading } from './Loading'

export function ServiceStatus() {
  const { state, refresh } = useReadiness()

  if (state.phase === 'loading') return <Loading label="Checking service status…" />

  if (state.phase === 'ready') {
    return (
      <section aria-labelledby="status-h">
        <h1 id="status-h">Service status</h1>
        <ul className="checks" role="status">
          <li>
            <span className="dot ok" aria-hidden="true" /> Backend: ready
          </li>
          <li>
            <span className="dot ok" aria-hidden="true" /> Database: ready
          </li>
        </ul>
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
