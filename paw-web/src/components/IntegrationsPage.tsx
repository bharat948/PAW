import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  calendarEvents,
  connectCalendarUrl,
  disconnectCalendar,
  listIntegrations,
  type CalendarEvent,
  type Integration,
} from '../api/client'
import { ErrorState } from './ErrorState'
import { Loading } from './Loading'

const CONNECT_ERRORS: Record<string, string> = {
  cancelled: 'Connecting was cancelled. Nothing was changed.',
  expired: 'That connection attempt expired. Please try again.',
  failed: 'Google Calendar could not be connected. Please try again.',
  missing_scope: 'Calendar access was not granted. Tick the calendar permission on the Google screen and try again.',
}

/** Reads (and removes) ?integration_error=... that the server appends when it sends the browser back from Google. */
function takeConnectError(): string | null {
  try {
    const url = new URL(window.location.href)
    const code = url.searchParams.get('integration_error')
    if (!code) return null
    url.searchParams.delete('integration_error')
    window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
    return CONNECT_ERRORS[code] ?? 'Connecting failed. Please try again.'
  } catch {
    return null
  }
}

const STATE_LABEL: Record<Integration['state'], string> = {
  connected: 'Connected',
  available: 'Not connected',
  needs_reauth: 'Needs reconnecting',
  unavailable: 'Unavailable',
}

function fmt(ev: CalendarEvent): string {
  if (!ev.start) return ''
  const d = new Date(ev.start)
  if (Number.isNaN(d.getTime())) return ev.start
  return ev.all_day
    ? d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) + ' · all day'
    : d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function Upcoming() {
  const [events, setEvents] = useState<CalendarEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    calendarEvents()
      .then((r) => alive && setEvents(r.events))
      .catch((e) => alive && setErr(e instanceof ApiError && e.status === 409 ? 'Reconnect to see events.' : 'Could not load events.'))
    return () => {
      alive = false
    }
  }, [])
  if (err) return <p className="muted" role="status">{err}</p>
  if (!events) return <p className="muted" role="status">Loading upcoming events…</p>
  if (events.length === 0) return <p className="muted">No events in the next 7 days.</p>
  return (
    <ul className="event-list" aria-label="Upcoming events">
      {events.map((e, i) => (
        <li key={e.id ?? i}>
          <strong>{e.title}</strong>
          <span className="muted">{fmt(e)}{e.location ? ` · ${e.location}` : ''}</span>
        </li>
      ))}
    </ul>
  )
}

function Card({ item, onChanged }: { item: Integration; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const isCal = item.id === 'google_calendar'

  async function disconnect() {
    if (!window.confirm('Disconnect Google Calendar? PAW will forget the connection and ask Google to revoke access.')) return
    setBusy(true)
    setError(null)
    try {
      await disconnectCalendar()
      onChanged()
    } catch {
      setError('Could not disconnect. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className="card integration" aria-labelledby={`int-${item.id}`}>
      <header>
        <h2 id={`int-${item.id}`}>{item.name}</h2>
        <span className={`pill ${item.state === 'connected' ? 'ok' : item.state === 'needs_reauth' ? 'warn' : ''}`}>{STATE_LABEL[item.state]}</span>
      </header>
      <p className="muted">{item.description}</p>
      {item.account_email && <p>Account: <strong>{item.account_email}</strong></p>}
      {item.state === 'unavailable' && item.reason && <p className="muted">{item.reason}</p>}
      <ul className="tool-list" aria-label={`${item.name} tools`}>
        {item.tools.map((t) => (
          <li key={t.name}>
            <code>{t.name}</code> <span className="pill">{t.access === 'read' ? 'read only' : 'can change things'}</span>
          </li>
        ))}
      </ul>
      {isCal && item.state === 'connected' && <Upcoming />}
      {error && <p role="alert" className="error-text">{error}</p>}
      {isCal && (
        <div className="actions">
          {(item.state === 'available' || item.state === 'needs_reauth') && (
            <a className="btn primary" href={connectCalendarUrl()}>
              {item.state === 'needs_reauth' ? 'Reconnect Google Calendar' : 'Connect Google Calendar'}
            </a>
          )}
          {(item.state === 'connected' || item.state === 'needs_reauth') && (
            <button type="button" onClick={disconnect} disabled={busy}>
              {busy ? 'Disconnecting…' : 'Disconnect'}
            </button>
          )}
        </div>
      )}
    </article>
  )
}

export function IntegrationsPage() {
  const [items, setItems] = useState<Integration[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [notice] = useState<string | null>(takeConnectError)

  const load = useCallback(() => {
    setFailed(false)
    listIntegrations()
      .then((r) => setItems(r.integrations))
      .catch(() => setFailed(true))
  }, [])
  useEffect(load, [load])

  return (
    <section aria-labelledby="integrations-h">
      <h1 id="integrations-h">Integrations</h1>
      <p className="muted">Connect services so PAW and Admin Hermès can use them. You can disconnect at any time.</p>
      {notice && <p role="alert" className="error-text">{notice}</p>}
      {failed ? (
        <ErrorState title="Could not load integrations" onRetry={load} />
      ) : !items ? (
        <Loading label="Loading integrations…" />
      ) : (
        <div className="integration-grid">
          {items.map((i) => (
            <Card key={i.id} item={i} onChanged={load} />
          ))}
        </div>
      )}
    </section>
  )
}
