# 002B build plan: the session ledger, context assembly and recovery

Implementation update (2026-11-04): all eight slices are built on `paw-002b-ledger`, including the approved task-checkpoint addition. Recovery, privacy and desktop/phone-width browser gates pass; local 002B acceptance is complete. See [implementation evidence](implementation-002b.md) and [operations](operations-002b.md).

Written 2026-11-04. Decisions: `plan-actor-foundation.md` section 7 (F1 to F4, L1 to L10). Spec: PAW-002 sections 8,
9, 12.4 and substory 002B (R01 to R06).

## What exists today (and is replaced)

- `agent_sessions(owner, workspace_id, title, status)`: one role only, no lineage, no deletion.
- History for a run = earlier SUCCEEDED runs' `user_message` and `output` (`lifecycle.history_for`). No tool results,
  no partial answers, no budget, no summaries.
- `agent_runs` and `agent_run_events` hold the text too (`user_message`, `output`, `message.delta` payloads), so
  "forget" and "delete" must reach them as well.
- Startup recovery marks every half-finished run INTERRUPTED without asking Hermès.

## Data model (migration 0011)

**Sessions** (extend `agent_sessions`)

| Column | Meaning |
|---|---|
| `owner` (existing) | data owner: the workspace owner |
| `requester_actor_id` | who asks in this session: `owner` for your chats, `admin-hermes` for a delegated task |
| `agent_actor_id` | the executor: the actor that answers |
| `kind` | `chat` (top level) or `delegation` |
| `forked_from_session_id`, `fork_seq` | history up to an edit point, read by reference from the parent |
| `delegated_from_session_id`, `delegated_from_run_id`, `delegated_from_invocation_id` | the assignment that created it |
| `root_session_id` | the top-level chat of a delegation tree (cost roll-up, deletion scope) |
| `title`, `title_source` (`auto`, `user`), `last_activity_at` | conversation list |
| `deleted_at`, `purge_after` | soft delete, purge after 30 days |

**Ledger** `agent_session_messages`: `id, session_id, seq` (unique, contiguous), `kind` (user, assistant, tool_call,
tool_result, platform_event, summary, delegation), `author_actor_id` (an actor or `system`), `run_id`,
`tool_invocation_id`, `status` (`complete`, `unfinished` for partial answers), `token_estimate`, `created_at`.
Rows are never updated or deleted outside maintenance (below).

**Content** `agent_message_contents(message_id, body, data jsonb)`: the text and structured payload, kept apart so
that forgetting removes content while the message row and its sequence stay (002B-R01 tombstone).

**Summaries** `agent_summaries(id, session_id, from_seq, to_seq, message_id, model, prompt_version, tokens_in,
tokens_out, invalidated_at)`: derived, never replaces ledger rows (R05). Forgetting a covered message invalidates it.

**Deletion log** `data_deletions(id, scope, session_id, message_id, requested_at, purged_at)`: no content; drives the
purge job and is re-applied after a restore from backup.

**Maintenance mode:** append-only triggers on the ledger, run events and terminal runs allow two narrow exceptions,
only inside a transaction that sets `paw.maintenance`: `redact` (blank content, nothing else) and `purge` (delete a
deleted session's rows). Normal code paths can never do either.

**Actors:** add kind `system` and a `system` actor for platform events.

## Who writes what

| Event | Writer | Ledger rows |
|---|---|---|
| You send a message | API admission, **same transaction as the run** | `user` |
| Hermès calls a tool | gateway (authoritative: it knows the decision and the real result) | `tool_call` + `tool_result` (or the denial) |
| Run ends | agent worker | `assistant` (`complete`, or `unfinished` with the partial text for INTERRUPTED/CANCELLED) |
| Summary made | summariser job | `summary` + `agent_summaries` row |
| Fork, forget, delete, delegation, actor lifecycle | API / services | `platform_event` or `delegation` |

## Context assembler v1 (spec 12.4)

Inputs: the session's ledger up to the current user message (including a fork's parent prefix), the latest valid
summary, the run's profile version. Output: `instructions` (profile text + operating notes + summary block) and
`conversation_history` in the compact-text form proven in S0 Q1 (`[tool NAME at TIME returned ...]` inside the
assistant turn). Rules:
- recent turns verbatim within a budget (24,000 tokens, lower for small-context models); older turns covered by the
  summary; if nothing covers them yet, the oldest are dropped and the summariser is scheduled;
- tool results above 2,000 tokens become a stub with a short excerpt and the result reference;
- tool results carry their timestamp, and the notes say snapshots of changing data must be fetched again;
- unfinished answers are marked, and the notes say only tool results prove that something happened;
- forgotten messages render as a tombstone; invalidated summaries are ignored;
- deterministic: the same inputs give byte-identical output and the same `assembly_hash`, stored on the run.

## Recovery (002B-R04)

At agent-worker start, before claiming work: for each run still DISPATCHING, RUNNING or CANCELLING, ask Hermès. If it
knows the run, resume the event stream from the last stored event (Hermès replays finished runs, verified), so a run
that actually completed is recorded SUCCEEDED from its real terminal event. If Hermès does not know it (container
recreated), the run is INTERRUPTED with its partial output. Stale leases are released. Approved operations are handed
to the operation reconciler when 002F exists.

## PR sequence

1. **Migration 0011 + schema tests:** session roles and lineage, ledger, contents, summaries, deletion log, maintenance
   mode on the new and existing append-only tables, `system` actor.
2. **Ledger service:** append with a per-session lock; admission writes the user message and the run in one
   transaction; the worker writes the assistant message (complete or unfinished); the gateway writes tool call and
   result rows; title and last activity maintained.
3. **Assembler v1:** rendering, budgets, stubs, summary use, markers, `assembly_hash`; property test over 200 generated
   sessions (R02); the worker switches from `history_for` to the assembler.
4. **Summariser:** job after a terminal run when the budget is exceeded; model configurable; usage recorded against
   the actor; invalidation on forget.
5. **Reconciler:** resume or interrupt at boot; stale leases; tests for kill-during-run (R04).
6. **Fork, forget, delete, purge:** fork at an edited message (no runs, no approvals copied); forget across ledger,
   run text, run events and summaries; delete with an explicit choice for delegated children; purge job; deletion
   log; restore re-applies deletions.
7. **API and web:** conversation list (rename, actor name), unfinished marker, summary card, tool results with time,
   forget button, edit-to-fork, delete dialog with backup expiry, delegated children nested (display only; creating
   them is the actor-runtime phase), All activity view for the owner.
8. **Real-container gates:** T205 (wipe Hermès state, recreate both containers, the next answer uses earlier facts),
   R04 with real kills, docs.

## Tests per requirement

| Req | Test |
|---|---|
| R01 | concurrent appends keep a contiguous sequence; forget leaves a tombstone and the sequence |
| R02 | 200 generated sessions: assembling twice gives byte-identical output and hash; any input change changes the hash |
| R03 / T205 | real container recreated with state wiped; earlier facts used |
| R04 | worker killed mid-run with Hermès still running: resumed and SUCCEEDED from the real terminal event; Hermès recreated: INTERRUPTED with partial output |
| R05 | long-session fixture: summary covers a range, originals untouched, inspector shows both |
| R06 | fork and diverge: child history equals the parent prefix; no tool call replayed; no approval carried |
| L5 | forget removes text from the next briefing, the summary and run text; purge removes every row of a deleted session after 30 days; restore re-applies |
| L8 hooks | parent task cost includes children (roll-up query) |

## Not in 002B

Delegation creation and the Actors page (actor-runtime phase), budget reservation and enforcement (cost phase; the
summariser records usage now), derived memories (002E: memory sources will point at ledger messages so forget can flag
them), the attention inbox (002F).
