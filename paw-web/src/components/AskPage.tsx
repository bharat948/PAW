import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  ApiError, streamChat, type DoneEvent, type RetrievalEvent,
} from '../api/client'
import { queryTerms } from '../lib/format'
import { CitedText } from './CitedText'
import { IconAlert, IconInfo, IconSend, IconSpark, IconStop } from './icons'
import { RetrievalTrace } from './RetrievalTrace'
import { SourceViewer } from './SourceViewer'

interface Msg {
  id: number
  role: 'user' | 'assistant'
  content: string
  retrieval?: RetrievalEvent
  done?: DoneEvent
  error?: string
  streaming?: boolean
}

const PROMPTS = [
  'Summarise what my documents are about',
  'Which documents mention revenue?',
  'What are the key dates or numbers mentioned?',
]
let nextId = 1

export function AskPage() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState<{ msgId: number; n: number } | null>(null)
  const abort = useRef<AbortController | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: 'end' })
  }, [msgs])
  useEffect(() => () => abort.current?.abort(), [])

  const patch = (id: number, p: Partial<Msg> | ((m: Msg) => Partial<Msg>)) =>
    setMsgs((xs) => xs.map((m) => (m.id === id ? { ...m, ...(typeof p === 'function' ? p(m) : p) } : m)))

  async function send(text: string) {
    text = text.trim()
    if (!text || busy) return
    const history = msgs.filter((m) => !m.error || m.content).map((m) => ({ role: m.role, content: m.content }))
    const aiId = nextId++
    setMsgs((xs) => [
      ...xs,
      { id: nextId++, role: 'user', content: text },
      { id: aiId, role: 'assistant', content: '', streaming: true },
    ])
    setInput('')
    setBusy(true)
    setOpen(null)
    const ctl = new AbortController()
    abort.current = ctl
    try {
      await streamChat(
        [...history.filter((h) => h.content), { role: 'user', content: text }],
        { topK: 6 },
        {
          onRetrieval: (retrieval) => patch(aiId, { retrieval }),
          onToken: (t) => patch(aiId, (m) => ({ content: m.content + t })),
          onDone: (done) => patch(aiId, { done }),
          onError: (e) => patch(aiId, { error: e.message }),
        },
        ctl.signal,
      )
    } catch (e) {
      patch(aiId, {
        error:
          e instanceof ApiError
            ? e.status === 429
              ? 'Too many questions. Please wait a moment and try again.'
              : e.message
            : 'Something went wrong.',
      })
    } finally {
      patch(aiId, { streaming: false })
      setBusy(false)
      abort.current = null
    }
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    void send(input)
  }
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send(input)
    }
  }

  const last = [...msgs].reverse().find((m) => m.retrieval)
  const opened = open ? msgs.find((m) => m.id === open.msgId) : undefined
  const openSource = opened?.retrieval?.sources.find((s) => s.n === open?.n)
  const terms = useMemo(() => queryTerms(opened?.retrieval?.query_used ?? ''), [opened])

  return (
    <>
      <div className="page-head row" style={{ justifyContent: 'space-between' }}>
        <div>
          <h1>Ask</h1>
          <p>Questions are answered only from your documents, with numbered citations you can open and verify.</p>
        </div>
        {msgs.length > 0 && (
          <button type="button" onClick={() => { abort.current?.abort(); setMsgs([]); setOpen(null) }}>
            New chat
          </button>
        )}
      </div>

      {last && !last.retrieval!.meta.generative && (
        <p className="notice info">
          <IconInfo size={18} />
          Offline demo mode: no language model is configured, so answers are the closest extracted passages rather
          than generated text. Retrieval, citations and the trace below are real.
        </p>
      )}

      <div className={`chat-layout${openSource ? ' with-source' : ''}`}>
        <div>
          {msgs.length === 0 && (
            <div className="card empty">
              <div className="empty-icon"><IconSpark size={28} /></div>
              <h2>Ask your documents anything</h2>
              <p>Each answer shows which passages were retrieved, and from where.</p>
              <div className="prompts">
                {PROMPTS.map((p) => (
                  <button key={p} type="button" onClick={() => void send(p)}>{p}</button>
                ))}
              </div>
            </div>
          )}

          <div className="thread" aria-live="polite" aria-label="Conversation">
            {msgs.map((m) =>
              m.role === 'user' ? (
                <div key={m.id} className="msg-user">{m.content}</div>
              ) : (
                <div key={m.id} className="msg-ai">
                  <div className="card bubble">
                    {m.content ? (
                      <CitedText
                        text={m.content}
                        nSources={m.retrieval?.sources.length ?? 0}
                        active={open?.msgId === m.id ? open.n : null}
                        onCite={(n) => setOpen({ msgId: m.id, n })}
                      />
                    ) : (
                      m.streaming && !m.error && <span className="muted">Searching your documents…</span>
                    )}
                    {m.streaming && <span className="caret" aria-hidden="true" />}
                    {m.error && (
                      <p role="alert" className="error-text" style={{ margin: m.content ? '.75rem 0 0' : 0 }}>
                        <IconAlert size={16} /> {m.error}
                      </p>
                    )}
                  </div>
                  {m.done && !m.done.grounded && m.done.reason === 'no_citations' && (
                    <p className="notice"><IconAlert size={18} /> This answer did not cite any source, so treat it with caution.</p>
                  )}
                  {m.retrieval && (
                    <RetrievalTrace
                      r={m.retrieval}
                      cited={m.done?.cited ?? []}
                      active={open?.msgId === m.id ? open.n : null}
                      onOpen={(n) => setOpen({ msgId: m.id, n })}
                    />
                  )}
                </div>
              ),
            )}
            <div ref={bottom} />
          </div>

          <div className="composer">
            <form onSubmit={submit} className="card">
              <label htmlFor="ask" className="visually-hidden">Ask a question</label>
              <textarea
                id="ask" rows={1} value={input} maxLength={4000} placeholder="Ask a question about your documents…"
                onChange={(e) => setInput(e.target.value)} onKeyDown={onKey}
              />
              {busy ? (
                <button type="button" className="stop" onClick={() => abort.current?.abort()}>
                  <IconStop size={16} /> Stop
                </button>
              ) : (
                <button type="submit" className="primary" disabled={!input.trim()}>
                  <IconSend size={16} /> Send
                </button>
              )}
            </form>
          </div>
        </div>
        {openSource && (
          <SourceViewer
            key={openSource.chunk_id} chunkId={openSource.chunk_id} terms={terms} onClose={() => setOpen(null)}
          />
        )}
      </div>
    </>
  )
}
