import type { LedgerPage, LedgerMessage } from '../api/client'

export function ConversationLedger({ page, onEdit, onForget, onOpen }: {
  page: LedgerPage; onEdit: (m: LedgerMessage) => void; onForget: (m: LedgerMessage) => void; onOpen: (id: string) => void
}) {
  return <>
    {page.summary_job?.error_code && <p role="status" className="muted">A summary is unavailable. Recent conversation history is still used.</p>}
    {page.messages.map((m) => {
      const summary = page.summaries.find((s) => s.message_id === m.id)
      const checkpoint = m.data?.type === 'task_checkpoint'
      if (m.kind === 'summary' && summary?.invalidated_at) return null
      return <article key={m.id} className={`ledger-message ledger-${m.kind}`} aria-label={`${m.kind.replace('_', ' ')} message`}>
        <div className="ledger-meta"><span>{m.author_actor_id}</span> <time dateTime={m.created_at}>{new Date(m.created_at).toLocaleString()}</time>{m.status === 'unfinished' && <strong> · Unfinished</strong>}</div>
        {m.forgotten ? <p className="muted">Message forgotten</p> : m.kind === 'summary' ? <details className="card"><summary>Conversation summary · messages {summary?.from_seq ?? m.data?.from_seq as number}–{summary?.to_seq ?? m.data?.to_seq as number}</summary><p>{m.body}</p></details>
          : checkpoint ? <details className="card"><summary>Task checkpoint · {String(m.data?.status)} (agent reported)</summary><p><strong>{String(m.data?.goal)}</strong></p><p>{m.body}</p>{Array.isArray(m.data?.next_steps) && <ul>{m.data.next_steps.map((step, i) => <li key={i}>{String(step)}</li>)}</ul>}</details>
            : m.kind === 'tool_call' || m.kind === 'tool_result' ? <details className="card"><summary>{String(m.data?.tool ?? 'Tool')} · {m.kind === 'tool_call' ? 'requested' : String(m.data?.decision ?? 'result')}</summary><p>{m.body}</p>{m.data?.result !== undefined && <pre>{JSON.stringify(m.data.result, null, 2)}</pre>}{m.kind === 'tool_call' && !page.messages.some((r) => r.kind === 'tool_result' && r.tool_invocation_id === m.tool_invocation_id) && <p>Outcome not recorded; do not assume this call succeeded.</p>}</details>
              : <div className={m.kind === 'user' ? 'msg-user' : 'bubble card'}>{m.body}</div>}
        {!m.forgotten && m.kind !== 'summary' && <div className="conversation-actions">
          {m.kind === 'user' && <button type="button" className="ghost" onClick={() => onEdit(m)}>Edit in a new branch</button>}
          <button type="button" className="ghost" onClick={() => onForget(m)}>Forget this message</button>
        </div>}
      </article>
    })}
    {page.children.length > 0 && <section aria-label="Delegated conversations"><h2>Delegated work</h2><ul>{page.children.map((s) => <li key={s.id}><button className="ghost" type="button" onClick={() => onOpen(s.id)}>{s.actor_name ?? s.agent_actor_id}: {s.title || 'Untitled task'}</button></li>)}</ul></section>}
  </>
}
