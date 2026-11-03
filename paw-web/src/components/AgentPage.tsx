import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  ApiError,
  RUN_TERMINAL,
  cancelAgentRun,
  createAgentSession,
  getAgentRun,
  getRuntimeHealth,
  listAgentRuns,
  streamRunEvents,
  submitAgentRun,
  type AgentEvent,
  type AgentRun,
  type RunState,
  type RuntimeHealth,
} from '../api/client'
import { IconAlert, IconSend, IconSpark, IconStop } from './icons'

const SESSION_KEY = 'paw.agent.session'
const store = {
  get: () => {
    try {
      return window.localStorage.getItem(SESSION_KEY)
    } catch {
      return null
    }
  },
  set: (v: string | null) => {
    try {
      if (v) window.localStorage.setItem(SESSION_KEY, v)
      else window.localStorage.removeItem(SESSION_KEY)
    } catch {
      /* private mode: the conversation just will not be remembered */
    }
  },
}

interface Tool {
  name: string
  done: boolean
  error: boolean
}
interface Turn {
  run: AgentRun
  live: string // text streamed so far; the stored output replaces it once the run is finished
  tools: Tool[]
  lost: boolean // the event stream could not be re-attached
}

const STATE_LABEL: Record<RunState, string> = {
  QUEUED: 'Waiting for its turn…',
  DISPATCHING: 'Starting…',
  RUNNING: 'Working…',
  CANCELLING: 'Stopping…',
  SUCCEEDED: '',
  FAILED: 'Something went wrong.',
  CANCELLED: 'Stopped.',
  INTERRUPTED: 'Interrupted. What was written is kept; send a new message to continue.',
}
const isDone = (s: RunState) => RUN_TERMINAL.includes(s)
const uid = () => (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `r${Date.now()}${Math.random().toString(16).slice(2)}`)
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

function sendError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return 'Admin Hermès is busy right now. Try again in a moment.'
    if (e.status === 409 && e.envelope?.code === 'session_busy') return 'Still working on your previous message.'
    if (e.status === 503) return 'Admin Hermès is not available.'
  }
  return 'Could not send your message. Please try again.'
}

export function AgentPage() {
  const [health, setHealth] = useState<RuntimeHealth | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const follow = useRef<AbortController | null>(null)
  const alive = useRef(true)
  const bottom = useRef<HTMLDivElement>(null)

  const patch = useCallback((id: string, f: (t: Turn) => Turn) => {
    if (alive.current) setTurns((ts) => ts.map((t) => (t.run.id === id ? f(t) : t)))
  }, [])

  /** Attach to a run's stored events; reconnect from the last sequence number until the run is finished. */
  const attach = useCallback(
    async (runId: string) => {
      follow.current?.abort()
      const ctl = new AbortController()
      follow.current = ctl
      let after = 0
      const onEvent = (e: AgentEvent) => {
        patch(runId, (t) => {
          if (e.type === 'message.delta') return { ...t, live: t.live + String(e.payload.text ?? '') }
          if (e.type === 'run.state') return { ...t, run: { ...t.run, state: e.payload.state as RunState } }
          if (e.type === 'tool.started')
            return { ...t, tools: [...t.tools, { name: String(e.payload.tool), done: false, error: false }] }
          if (e.type === 'tool.completed') {
            const idx = t.tools.findIndex((x) => x.name === e.payload.tool && !x.done)
            const tools = t.tools.map((x, i) => (i === idx ? { ...x, done: true, error: e.payload.error === true } : x))
            return { ...t, tools }
          }
          return t
        })
      }
      for (let attempt = 0; attempt < 6 && !ctl.signal.aborted; attempt++) {
        try {
          after = await streamRunEvents(runId, after, onEvent, ctl.signal)
          const run = await getAgentRun(runId)
          patch(runId, (t) => ({ ...t, run, lost: false }))
          if (isDone(run.state)) return
        } catch {
          if (ctl.signal.aborted) return
        }
        await wait(Math.min(4000, 400 * 2 ** attempt))
      }
      if (!ctl.signal.aborted) patch(runId, (t) => ({ ...t, lost: true }))
    },
    [patch],
  )

  const loadSession = useCallback(
    async (id: string) => {
      const { runs } = await listAgentRuns(id)
      if (!alive.current) return
      setSessionId(id)
      setTurns(runs.map((run) => ({ run, live: '', tools: [], lost: false })))
      const active = runs.find((r) => !isDone(r.state))
      if (active) void attach(active.id) // navigated away mid-run: pick the run back up
    },
    [attach],
  )

  useEffect(() => {
    alive.current = true
    ;(async () => {
      try {
        setHealth(await getRuntimeHealth())
        const saved = store.get()
        if (saved) {
          try {
            await loadSession(saved)
            return
          } catch (e) {
            if (!(e instanceof ApiError && e.status === 404)) throw e
            store.set(null)
          }
        }
      } catch {
        if (alive.current) setError('Could not reach Admin Hermès. Please try again.')
      } finally {
        if (alive.current) setLoading(false)
      }
    })()
    return () => {
      alive.current = false
      follow.current?.abort()
    }
  }, [loadSession])

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: 'end' })
  }, [turns])

  const active = turns.find((t) => !isDone(t.run.state))

  async function send(text: string) {
    text = text.trim()
    if (!text || active) return
    setError(null)
    try {
      let id = sessionId
      if (!id) {
        id = (await createAgentSession()).id
        store.set(id)
        setSessionId(id)
      }
      const run = await submitAgentRun(id, uid(), text)
      setInput('')
      setTurns((ts) => [...ts, { run, live: '', tools: [], lost: false }])
      void attach(run.id)
    } catch (e) {
      setError(sendError(e))
    }
  }

  async function cancel(runId: string) {
    try {
      const run = await cancelAgentRun(runId)
      patch(runId, (t) => ({ ...t, run }))
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? 'It is just starting. Try Stop again in a moment.' : 'Could not stop the run.')
    }
  }

  function newChat() {
    follow.current?.abort()
    store.set(null)
    setSessionId(null)
    setTurns([])
    setError(null)
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send(input)
    }
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    void send(input)
  }

  const unavailable = health !== null && !health.configured
  const notReady = health !== null && health.configured && !health.ready

  return (
    <section aria-labelledby="agent-h">
      <div className="agent-head">
        <h1 id="agent-h">Admin Hermès</h1>
        {turns.length > 0 && (
          <button type="button" className="ghost" onClick={newChat} disabled={!!active}>
            New conversation
          </button>
        )}
      </div>
      <p className="muted">Chat with PAW&apos;s administrative agent. Every message and step is stored, so you can leave and come back.</p>
      {unavailable && (
        <p role="alert" className="error-text">
          Admin Hermès is not configured on this server (PAW_HERMES_URL and PAW_HERMES_KEY).
        </p>
      )}
      {notReady && (
        <p role="alert" className="error-text">
          Admin Hermès is not ready{health.missing.length ? ` (${health.missing.join(', ')})` : ''}. Is the container running?
        </p>
      )}

      <div className="agent-thread" aria-live="polite">
        {loading && <p role="status" className="muted">Loading…</p>}
        {!loading && turns.length === 0 && !unavailable && (
          <div className="card empty-hint">
            <IconSpark size={20} /> Ask something to get started. For example: &ldquo;What can you do?&rdquo;
          </div>
        )}
        {turns.map((t) => {
          const text = isDone(t.run.state) ? (t.run.output ?? t.live) : t.live
          return (
            <div key={t.run.id} className="agent-turn">
              <div className="msg-user">{t.run.message}</div>
              <div className="msg-ai">
                {t.tools.length > 0 && (
                  <ul className="tool-activity" aria-label="Tool activity">
                    {t.tools.map((x, i) => (
                      <li key={i}>
                        <code>{x.name}</code> {x.done ? (x.error ? 'failed' : 'done') : 'running…'}
                      </li>
                    ))}
                  </ul>
                )}
                {text && <div className="bubble card">{text}</div>}
                <p className="muted small agent-status" role="status">
                  {t.lost ? 'Lost the connection to the live stream. Reload to see the latest.' : STATE_LABEL[t.run.state]}
                  {t.run.state === 'FAILED' && t.run.error_code ? ` (${t.run.error_code})` : ''}
                </p>
                {!isDone(t.run.state) && t.run.state !== 'CANCELLING' && (
                  <button type="button" className="stop" onClick={() => void cancel(t.run.id)}>
                    <IconStop size={14} /> Stop
                  </button>
                )}
              </div>
            </div>
          )
        })}
        <div ref={bottom} />
      </div>

      {error && (
        <p role="alert" className="error-text">
          <IconAlert size={16} /> {error}
        </p>
      )}
      <div className="composer">
        <form className="card" onSubmit={submit}>
          <label htmlFor="agent-input" className="sr-only">
            Message Admin Hermès
          </label>
          <textarea
            id="agent-input"
            rows={1}
            value={input}
            maxLength={8000}
            placeholder={active ? 'Waiting for the answer…' : 'Message Admin Hermès'}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            disabled={unavailable}
          />
          <button type="submit" className="primary" disabled={!input.trim() || !!active || unavailable} aria-label="Send">
            <IconSend size={16} /> Send
          </button>
        </form>
      </div>
    </section>
  )
}
