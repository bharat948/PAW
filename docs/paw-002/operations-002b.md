# Operating durable conversations

Run `make migrate` in `paw-server` before starting the API, agent worker or session worker. `./dev.sh` starts the separate session maintenance worker. For another process supervisor, start `python -m paw_server.session_worker` alongside `python -m paw_server.agent_worker`; restarting maintenance does not interrupt chat. It performs journal replay, summary jobs and deletion purges. Run a single agent worker per runtime.

## Context and task checkpoints

The pinned actor profile asks for a structured private task checkpoint at the end of each answer. Checkpoints identify a task, its state, outcome, evidence IDs and next steps. They are agent claims; they never grant permissions or prove tool execution. Complete-task boundaries are preferred for summarization. A size fallback handles tasks that remain open. Original messages remain in PostgreSQL until explicitly forgotten or purged.

Defaults are 24,000 recent-history tokens, 1,200 summary tokens and 2,000 tokens before a tool result becomes a reference stub. Set `PAW_AGENT_HISTORY_TOKENS`, `PAW_AGENT_SUMMARY_TOKENS` and `PAW_AGENT_TOOL_HISTORY_TOKENS` for smaller models. The deterministic estimator and assembler are versioned. Run metadata records the selected messages, profile, revisions, summary reference and assembly hash.

Summaries use the server's existing LLM endpoint/key configuration with `PAW_AGENT_SUMMARY_MODEL=gpt-4o-mini` by default. The maintenance worker records the actual model and usage, retries transient failures, and rejects stale results. Unconfigured providers or exhausted retries produce visible summary failures while recent history remains available. Summaries are budgeted like run calls; see [operations-a1.md](operations-a1.md).

## Deletion journal and backups

Set `PAW_DELETION_JOURNAL_DIR` to a durable absolute directory outside PostgreSQL and Hermes storage. The default is `$XDG_STATE_HOME/paw/deletions`, or `~/.local/state/paw/deletions`. All PAW processes must share that same directory. Do not discard it during upgrades or restores. A deletion or forget request is written atomically and synchronized to local disk before acknowledgement. Startup and privacy-sensitive reads replay receipts idempotently.

Deletion hides a conversation immediately and schedules live-data purge after 30 days. The delete dialog requires a choice for delegated children. Surviving forks keep their inherited prefix; purge materializes it and retains source IDs. Explicit forget follows those IDs across branches. Backups retain older copies until their retention expiry, so restores must apply the current journal before serving requests.

Configure the existing backup destination, `PAW_HOME_ENV` (including `PAW_INSTALLATION_ID`) and `PAW_BACKUP_AGE_RECIPIENT`, then run from the repository root:

```sh
uv run --project paw-server --with pyrage python scripts/backup/pawbackup.py sync-deletions --watch
```

The watcher retries every 60 seconds, encrypts each snapshot, verifies its uploaded bytes, then publishes the synchronization watermark. `./dev.sh` starts it automatically when both `PAW_HOME_ENV` and `PAW_BACKUP_AGE_RECIPIENT` are exported. A deployed supervisor must run it continuously. A normal database backup also synchronizes receipts. Inspect `sync-status.json` in the journal directory or the backup destination's `privacy/<installation>/latest.json` for the last successful watermark. Off-host recovery covers requests through that watermark; unsynchronized local receipts require the surviving local journal.

Export a current local journal, then supply it to every restore or drill:

```sh
uv run --project paw-server --with pyrage python scripts/backup/pawbackup.py export-deletions --output /secure/current-deletions.json
uv run --project paw-server --with pyrage python scripts/backup/pawbackup.py drill BACKUP_ID --identity /secure/age-identity --deletion-journal /secure/current-deletions.json
```

For off-host recovery, download the encrypted snapshot named in `latest.json`, verify its SHA-256, and supply that `.json.age` file as `--deletion-journal`. The restore requires the journal, checks installation identity where supplied, migrates older schemas, reapplies deletions, and reports the watermark before exposing restored data. Keep the private age identity outside the installation.

## Recovery and acceptance

Worker startup reconciles known runtime IDs before claiming queued work. It resumes stored source sequences and cancellation intent. Temporary outages receive bounded retries (`PAW_AGENT_RECOVERY_DEADLINE_S`, default 30 seconds). Missing runtimes and unknown dispatch outcomes become interrupted with preserved partial output; recovery never creates a replacement execution.

Focused tests use disposable PostgreSQL databases. `PAW_LEDGER_REAL=1` opts into `tests/sessions/test_container_recovery.py`, using fresh `paw-002b-test-*` containers and previously authorized S0 credentials. No volumes are mounted. Never run destructive container tests against the owner runtime. Browser acceptance uses `npm run test:ledger` with `PAW_TEST_DATABASE_URL` and starts its own API/database on ports 18002/15174.

Home deployment, public HTTPS/phone acceptance, egress enforcement, actor creation, memory, approvals and GitHub remain subsequent phases. Cost controls are in [operations-a1.md](operations-a1.md).
