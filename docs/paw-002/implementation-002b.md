# PAW 002B implementation evidence

Implementation on `paw-002b-ledger`, 2026-11-04. All eight slices are implemented and local 002B acceptance passes. Hosted phone acceptance remains part of M1 deployment.

## Review slices and requirement coverage

| Slice | Implementation | Evidence |
|---|---|---|
| 1. Schema and backfill | `0011_session_ledger.py`, schema tests; subsequent `0012`/`0013` extend jobs and privacy | 70 initial migration/lifecycle checks; backfill, invariant and upgrade/downgrade coverage in `tests/test_ledger_schema.py` |
| 2. Atomic writers | `modules/sessions/service.py`, `runtime/lifecycle.py`, `gateway/service.py` | concurrent contiguous appends; duplicate admission/event recovery; rollback without orphan runs/messages/events; one terminal assistant; full tool results stored separately; interrupted calls retain unknown outcome |
| 3. Context assembly | `modules/sessions/assembler.py` | 200 generated session fixtures in `test_assembler.py`, deterministic bytes/hash, forks, tombstones, unfinished answers and long tool results; current input appears once; dispatch privacy fence |
| 4. Task compaction and jobs | `checkpoints.py`, `summaries.py`, additive LLM usage interface, separate session worker | task-completion boundary below size limit; size fallback; incremental summary; deduplicated persistent jobs, bounded retries, in-flight forget rejects stale result; actual returned model/token usage |
| 5. Recovery | `runtime/agent_worker.py`, `hermes_adapter.py` | fake network/missing/cancellation/replay coverage; real worker SIGKILL recovery succeeds exactly once; runtime recreation keeps partial output as unfinished |
| 6. Privacy and restore | `privacy.py`, `journal.py`, `session_restore.py`, backup tooling | fork retention through parent purge, source-ID forget propagation, explicit delegated deletion choices, late tool-result discard, journal survives DB rollback, older DB snapshot replay, encrypted upload/hash verification/corruption rejection |
| 7. API and chat controls | `api/agent.py`, conversation components and client | owner/workspace scoping, paginated messages/list/activity, CSRF; desktop and Pixel 5 browser flows for reopen, rename, edit-to-fork, forget, activity and delete; Escape/focus handling and horizontal overflow check |
| 8. Acceptance and operations | tests, CI and `operations-002b.md` | final check results below; deterministic gates in CI; real-container tests require explicit opt-in and disposable state |

The owner-approved task-oriented compaction uses structured `in_progress`, `completed` and `blocked` checkpoints at the end of answers. Completing an inference does not complete a task. Checkpoints remain agent claims; stored tool evidence remains authoritative. Size limits still apply to long-running tasks. Original messages are preserved until forgotten or purged.

## Observed acceptance results

- Schema baseline: **70 passed** against disposable PostgreSQL databases.
- Final targeted ledger, privacy, summary, runtime and API regression run: **107 passed**. Additional privacy/transaction checks: **19 passed**, including cookie CSRF, delegated deletion choices and forgetting during a tool invocation.
- Deterministic assembler: **4 passed**, including a loop over **200 generated sessions**. Additive completion interface: verifies the returned model/token usage and the existing text-only interface.
- Backup encryption: **3 passed**, with the real age encryption library and a controlled object-store transport. Full off-host deployment remains a later phase.
- Web unit tests: **60 passed**; typecheck and production build passed. Lint has the existing warnings recorded by its output.
- Browser acceptance: **4 passed (19.5 seconds)**, desktop and Pixel 5, against a disposable API/database. Both layouts also reload during a held active stream, reconnect from stored events, and finish with one assistant/user message. The shared-data phone run exposed a conversation-list overflow/sticky-composer overlap; fixed with a stacked phone list and a composer in normal phone document flow.
- Real T205: **passed**. Recreated PAW API/worker objects and Hermes with no mounted state; the next inference recalled a random fictional garden nickname from PostgreSQL. The assembled request hash matched the pre-restart hash.
- Real recovery gates: **2 passed (153.85 seconds)**. Killed an actual one-shot worker process while Hermes retained the run, recovered its real terminal result with one assistant row; killed a worker after stored text deltas, recreated Hermes without state, and retained the answer as unfinished/interrupted. Repeated recovery was idempotent.
- Server lint and typecheck: passed (96 source files). Full regression: **794 passed, 1 skipped, 26 deselected** in 437 seconds; three existing ingestion-test connection-pool cleanup warnings. Later focused verification covers the final privacy fixes (**13 passed**), summary follow-up scheduling (**6 passed**), completion usage (**1 passed**) and timestamped assembly (**4 passed**). The skipped optional encryption tests were separately run with pyrage (**3 passed**); provider/real-runtime tests are excluded from the deterministic suite.

Test transcripts are local working evidence in `/tmp/paw-002b-*.log`; reproducible commands and durable acceptance tests live in this repository. CI runs deterministic server and ledger browser checks; it does not consume provider credentials. Isolated live tests reuse the existing authorized S0 configuration without printing credentials or mounting owner state.

## Review commits

| Slice | Commit | Primary evidence |
|---|---|---|
| 1 | `c4f3697` | [Schema and migration tests](../../paw-server/tests/test_ledger_schema.py) |
| 2 | `826e300` | [Atomic ledger tests](../../paw-server/tests/sessions/test_ledger.py) |
| 3 | `e6959c5` | [Generated deterministic assembly tests](../../paw-server/tests/sessions/test_assembler.py) |
| 4 | `6ca2c60` | [Summary jobs and races](../../paw-server/tests/sessions/test_summaries_recovery.py), [completion usage](../../paw-server/tests/sessions/test_completion.py) |
| 5 | `686c2d9` | [Isolated real worker/container gates](../../paw-server/tests/sessions/test_container_recovery.py) |
| 6 | `25fe56c` | [Privacy and restore tests](../../paw-server/tests/sessions/test_privacy.py), [encrypted journal tests](../../paw-server/tests/sessions/test_backup_journal.py) |
| 7 | `e2b6e61` | [Desktop/phone browser acceptance](../../paw-web/e2e-ledger/conversations.spec.ts) |
| 8 | Final acceptance commit | This coverage record, test fixtures, CI and [operations](operations-002b.md) |

These commits form one dependent implementation stack; the reported final verification applies to the complete stack.

## Remaining M1 and Story 2 work

Cost accounting/enforcement, full runtime timeouts, the served model report, home compose/release packaging, continuous backup/journal supervision on the home host, HTTPS and hosted phone acceptance, and outbound network enforcement remain subsequent work. Google Calendar still needs the owner’s real OAuth click-through acceptance. Actor creation/delegation runtime, memory, approvals, GitHub and evaluation remain later phases. 002B does not complete M1 or the whole story.

See [operations-002b.md](operations-002b.md) for worker configuration, journal storage, encrypted synchronization every 60 seconds, restore requirements and recovery commands. Off-host recovery covers deletion requests through the last successful journal synchronization watermark.
