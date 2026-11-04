import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  ApiError,
  allAgentMessages, listAgentSessions, listAgentActivity, renameAgentSession, forkAgentSession, forgetAgentMessage, deleteAgentSession,
  type AgentSession, type LedgerPage, type LedgerMessage,
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
import { ConversationDialog } from './ConversationDialog'
import { ConversationLedger } from './ConversationLedger'
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
interface Decision {
  tool: string
  decision: 'allowed' | 'denied' | 'error'
  reason: string
}
interface Turn {
  run: AgentRun
  live: string // text streamed so far; the stored output replaces it once the run is finished
  tools: Tool[]
  decisions: Decision[] // what the PAW gateway decided for each tool call: the evidence, not the model's words
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
const REASON_TEXT: Record<string, string> = {
  OK: 'allowed',
  NOT_CONNECTED: 'denied: not connected',
  CONNECTION_REVOKED: 'denied: needs reconnecting',
  NOT_GRANTED: 'denied: not allowed for this agent',
  UNKNOWN_TOOL: 'denied: no such tool',
  TOOL_DISABLED: 'denied: switched off',
  NATIVE_TOOL_BLOCKED: 'blocked: not a PAW tool',
  INVALID_ARGUMENTS: 'denied: invalid request',
  RATE_LIMITED: 'denied: too many calls in this run',
  BUDGET_EXCEEDED: 'denied: budget used up',
  APPROVAL_REQUIRED: 'denied: needs your approval',
  NO_ACTIVE_RUN: 'denied: no active run',
  ATTESTATION_MISSING: 'denied: runtime not verified',
  ATTESTATION_MISMATCH: 'denied: runtime does not match its profile',
  TOOL_ERROR: 'failed',
  HANDLER_ERROR: 'failed',
}
export const decisionText = (d: Decision) => REASON_TEXT[d.reason] ?? (d.decision === 'allowed' ? 'allowed' : `denied: ${d.reason}`)
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
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [ledger, setLedger] = useState<LedgerPage>({ messages: [], summaries: [], children: [], summary_job: null, next_after: null })
  const [dialog, setDialog] = useState<{ kind: 'rename' | 'edit' | 'forget' | 'delete'; message?: LedgerMessage; requestId?: string } | null>(null)
  const [activity, setActivity] = useState<{ messages: LedgerMessage[]; next_cursor: string | null } | null>(null)
  const selected = useRef<string | null>(null)
  const loadVersion = useRef(0)
  const follow = useRef<AbortController | null>(null)
  const alive = useRef(true)
  const bottom = useRef<HTMLDivElement>(null)

  const patch = useCallback((id: string, f: (t: Turn) => Turn) => {
    if (alive.current) setTurns((ts) => ts.map((t) => (t.run.id === id ? f(t) : t)))
  }, [])

  const refreshLedger = useCallback(async (id: string | null) => {
    const version = ++loadVersion.current
    const [list, page] = await Promise.all([listAgentSessions(), id ? allAgentMessages(id) : Promise.resolve(null)])
    if (!alive.current || version !== loadVersion.current) return
    setSessions(list.sessions)
    if (selected.current === id && page) setLedger(page)
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
          if (e.type === 'tool.decision')
            return {
              ...t,
              decisions: [
                ...t.decisions,
                { tool: String(e.payload.tool), decision: e.payload.decision as Decision['decision'], reason: String(e.payload.reason ?? '') },
              ],
            }
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
          if (isDone(run.state)) {
            await refreshLedger(selected.current)
            return
          }
        } catch {
          if (ctl.signal.aborted) return
        }
        await wait(Math.min(4000, 400 * 2 ** attempt))
      }
      if (!ctl.signal.aborted) patch(runId, (t) => ({ ...t, lost: true }))
    },
    [patch, refreshLedger],
  )

  const loadSession = useCallback(
    async (id: string) => {
      const version = ++loadVersion.current
      selected.current = id
      follow.current?.abort()
      const [{ runs }, page] = await Promise.all([listAgentRuns(id), allAgentMessages(id)])
      if (!alive.current || version !== loadVersion.current) return
      setActivity(null)
      setLedger(page)
      store.set(id)
      setSessionId(id)
      setTurns(runs.map((run) => ({ run, live: '', tools: [], decisions: [], lost: false })))
      const active = runs.find((r) => !isDone(r.state))
      if (active) void attach(active.id) // navigated away mid-run: pick the run back up
    },
    [attach],
  )

  useEffect(() => {
    alive.current = true
    ;(async () => {
      try {
        const [healthResult, list] = await Promise.all([getRuntimeHealth(), listAgentSessions()])
        setHealth(healthResult)
        setSessions(list.sessions)
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
        selected.current = id
      }
      const run = await submitAgentRun(id, uid(), text)
      setInput('')
      setTurns((ts) => [...ts, { run, live: '', tools: [], decisions: [], lost: false }])
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
    selected.current = null
    loadVersion.current++
    setLedger({ messages: [], summaries: [], children: [], summary_job: null, next_after: null })
    setActivity(null)
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

  async function changeConversation(value: string, include: boolean) {
    if (!sessionId || !dialog) return
    if (dialog.kind === 'rename') await renameAgentSession(sessionId, value.trim())
    else if (dialog.kind === 'delete') {
      await deleteAgentSession(sessionId, include)
      newChat()
      await refreshLedger(null)
      return
    } else if (dialog.kind === 'forget' && dialog.message) {
      await forgetAgentMessage(sessionId, dialog.message.id)
      await loadSession(sessionId)
    } else if (dialog.kind === 'edit' && dialog.message) {
      const fork = await forkAgentSession(sessionId, dialog.message.id, value.trim(), dialog.requestId!)
      await loadSession(fork.session.id)
      await refreshLedger(fork.session.id)
      return
    }
    await refreshLedger(selected.current)
  }
  const openConversation = (id: string) => { void loadSession(id).catch(() => setError('Could not open that conversation.')) }
  const showActivity = async (more = false) => {
    const version = ++loadVersion.current
    try {
      const page = await listAgentActivity(more ? activity?.next_cursor ?? undefined : undefined)
      if (!alive.current || version !== loadVersion.current) return
      setActivity((old) => ({ ...page, messages: more && old ? [...old.messages, ...page.messages] : page.messages }))
    } catch { setError('Could not load all activity.') }
  }

  const unavailable = health !== null && !health.configured
  const notReady = health !== null && health.configured && !health.ready

  return (
    <section aria-labelledby="agent-h">
      <div className="agent-head">
        <h1 id="agent-h">Admin Hermès</h1>

      </div>
      <p className="muted">Chat with PAW&apos;s administrative agent. Every message and step is stored, so you can leave and come back.</p>
      {unavailable && (
        <p role="alert" className="error-text">
          Admin Hermès is not configured on this server (PAW_HERMES_URL and PAW_HERMES_KEY).
        </p>
      )}
      {notReady && (
        <p role="alert" className="error-text">
          {health.missing.some((m) => m.startsWith('config_'))
            ? "Admin Hermès's container does not match its profile. Restart it (./dev.sh) to apply the profile."
            : `Admin Hermès is not ready${health.missing.length ? ` (${health.missing.join(', ')})` : ''}. Is the container running?`}
        </p>
      )}

      <details className="conversation-list" open>
        <summary>Conversations</summary>
        <nav aria-label="Conversations"><ul>{sessions.map((s) => <li key={s.id}><button type="button" className="ghost" aria-current={s.id === sessionId ? 'page' : undefined} onClick={() => openConversation(s.id)}>{s.title || 'Untitled conversation'} <span className="muted">· {s.actor_name ?? s.agent_actor_id}</span></button></li>)}</ul></nav>
        <div className="conversation-actions"><button type="button" className="ghost" onClick={newChat}>New conversation</button><button type="button" className="ghost" onClick={() => void showActivity()}>All activity</button>
          {sessionId && <><button type="button" className="ghost" onClick={() => setDialog({ kind: 'rename' })}>Rename conversation</button><button type="button" className="ghost" onClick={() => setDialog({ kind: 'delete' })}>Delete conversation</button></>}
        </div>
      </details>
      {activity ? <section aria-label="All activity"><h2>All activity</h2><button type="button" onClick={() => setActivity(null)}>Back to conversation</button><ul>{activity.messages.map((m) => <li key={m.id}><button type="button" className="ghost" onClick={() => openConversation(m.session_id)}>{m.author_actor_id} · {m.kind.replace('_', ' ')} · {m.forgotten ? 'Message forgotten' : (m.body ?? '').slice(0, 180)}</button></li>)}</ul>{activity.next_cursor && <button type="button" onClick={() => void showActivity(true)}>Load more activity</button>}</section> : <div className="agent-thread" aria-live="polite">
        <ConversationLedger page={ledger} onOpen={openConversation} onEdit={(message) => setDialog({ kind: 'edit', message, requestId: uid() })} onForget={(message) => setDialog({ kind: 'forget', message })} />
        {loading && <p role="status" className="muted">Loading…</p>}
        {!loading && turns.length === 0 && !unavailable && (
          <div className="card empty-hint">
            <IconSpark size={20} /> Ask something to get started. For example: &ldquo;What can you do?&rdquo;
          </div>
        )}
        {turns.filter((t) => !isDone(t.run.state) || !ledger.messages.some((m) => m.run_id === t.run.id)).map((t) => {
          const text = isDone(t.run.state) ? (t.run.output ?? t.live) : t.live
          return (
            <div key={t.run.id} className="agent-turn">
              {!ledger.messages.some((m) => m.run_id === t.run.id && m.kind === 'user') && <div className="msg-user">{t.run.message}</div>}
              <div className="msg-ai">
                {t.decisions.length > 0 ? (
                  <ul className="tool-activity" aria-label="Tool activity">
                    {t.decisions.map((d, i) => (
                      <li key={i} className={d.decision === 'allowed' ? '' : 'tool-denied'}>
                        <code>{d.tool}</code> {decisionText(d)}
                      </li>
                    ))}
                  </ul>
                ) : (
                  t.tools.length > 0 && (
                    <ul className="tool-activity" aria-label="Tool activity">
                      {t.tools.map((x, i) => (
                        <li key={i}>
                          <code>{x.name}</code> {x.done ? (x.error ? 'failed' : 'done') : 'running…'}
                        </li>
                      ))}
                    </ul>
                  )
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
      </div>}

      {error && (
        <p role="alert" className="error-text">
          <IconAlert size={16} /> {error}
        </p>
      )}
      {dialog && <ConversationDialog key={`${dialog.kind}-${dialog.message?.id ?? sessionId}`} title={dialog.kind === 'rename' ? 'Rename conversation' : dialog.kind === 'edit' ? 'Edit in a new branch' : dialog.kind === 'forget' ? 'Forget this message?' : 'Delete conversation?'}
        initial={dialog.kind === 'edit' ? dialog.message?.body ?? '' : dialog.kind === 'rename' ? sessions.find((s) => s.id === sessionId)?.title ?? '' : ''}
        inputLabel={dialog.kind === 'edit' ? 'Replacement message' : dialog.kind === 'rename' ? 'Conversation title' : undefined}
        confirm={dialog.kind === 'delete' ? 'Delete conversation' : dialog.kind === 'forget' ? 'Forget message' : dialog.kind === 'edit' ? 'Create branch and send' : 'Save title'} onConfirm={changeConversation} onClose={() => setDialog(null)}>
        {dialog.kind === 'edit' && <p>The original conversation stays intact. Earlier messages carry into a new branch; past tool calls are never run again.</p>}
        {dialog.kind === 'forget' && <p>This removes the message and related response and tool content from future context, including inherited branches and summaries. Requests already sent to a provider cannot be recalled. Older backup copies expire in up to about 35 days; restore reapplies the deletion record.</p>}
        {dialog.kind === 'delete' && <p>This conversation is hidden immediately and purged after 30 days. Existing forks keep their history. Older backup copies expire in up to about 35 days; restore reapplies the deletion record.</p>}
      </ConversationDialog>}
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
