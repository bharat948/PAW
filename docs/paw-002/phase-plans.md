# PAW-002 phase engineering plans

Spec: `PAW_002_Admin_Hermes_Harness_Requirements_v2.docx` (v2.0). Companion to [plan.md](plan.md) (status, findings, S0 inputs).
Written 2026-11-02 for Bharat and implementers. Sizes are the spec's relative sizes (S/M), not day estimates.
Nothing below is built unless stated; S0 has not been run against a real Hermès yet.

## 0. Working rules for every phase

1. **Tests ship with the code.** A phase's PRs are green on lint, types, unit and integration tests before merge. Each PR lists which spec requirement IDs (e.g. 002A-R03) and test IDs (T203) it covers.
2. **Small PRs**, each reviewable from a phone (spec 16). Target one table group, one module or one endpoint family per PR.
3. **Evidence note per phase** in the PR (what was tested, what was not), like `docs/environment-contract.md` for 001D.
4. **No stub that passes vacuously.** A test that cannot run (needs real Hermès or a model) is marked and skipped, never faked green.
5. **Stop rule from the spec:** if S0 Q1 or Q2 fails, pause for a re-plan decision with Bharat.

## 1. Conventions this repo already has (and how PAW-002 adopts them)

| Topic | Existing | PAW-002 decision (needs ADR-204 "conventions") |
|---|---|---|
| Ids | UUID (`gen_random_uuid()`), actor ids are text (`'owner'`) | UUID; keep `'owner'` as the human actor id; add `'admin-hermes'`. Spec says ULID, deviation recorded |
| Schema | One schema, raw SQL via sync `psycopg` pool, Alembic `0001..0004` | Same. New tables prefixed by module (`agent_`, `memory_`, `gateway_`, `approval_`, `connector_`, `ops_`). Migrations `0005+` |
| API | Sync FastAPI routers in `api/`, `CurrentActor` bearer dependency, error envelope `{code,message,correlation_id,retryable}` | Same. New routers `api/agent.py`, `api/approvals.py`, `api/memory.py`, `api/profiles.py`, `api/connections.py` |
| Streaming | `StreamingResponse` SSE generator (`api/chat.py`), `X-Accel-Buffering: no` | Same pattern, plus replay from PostgreSQL |
| Background | Sync polling worker with lease fencing (`ingestion/jobs.py`) | New `agent_worker.py` entrypoint (same image, separate process) so a 10-minute run never blocks ingestion |
| Config | `PAW_*` in `core/config.py`, validated at startup | New `PAW_HERMES_*`, `PAW_AGENT_*`; validated, `home` env rejects placeholders (extend `_check_home`) |
| Logging | Structured JSON, correlation id | Add run id and trace id; log scan test excludes bodies (002H-R02) |
| Tests | `tests/` unit; `integration` marker needs `PAW_TEST_DATABASE_URL`; `tests/contract/hermes` fake-or-real | Same markers. New: `tests/agent/`, `tests/gateway/`, `tests/memory/`, `tests/approvals/`, `tests/evals/` |

## 2. S0: integration spike (time-boxed; needs real image + OpenAI key)

**Goal:** prove Hermès behaves as the design assumes, or change the design before writing production code.

**Inputs:** image `paw-hermes:s0` (built from `deploy/hermes/`), OpenAI key (interim, see plan.md section 3), `gpt-4o-mini` or another 64k+ context model.

**Steps**
1. Start: `docker compose --env-file <outside-repo>/s0.env -f deploy/hermes/compose.s0.yaml up -d`; confirm `GET /v1/capabilities` with the key; confirm 401 without it.
2. Discover config: `hermes config --help`, `hermes model`, read the pinned checkout's docs/source for the real `openai-api` provider keys and for `shared metrics` off switch. Write the exact settings into `deploy/hermes/` (entrypoint) and ADR-203.
3. Run `PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY=... PAW_HERMES_CAPTURE_DIR=docs/paw-002/captures uv run pytest tests/contract/hermes`.
4. Capture real SSE streams for: plain answer, tool use, stop, failure. Record event names, ids, ordering, usage, served model (Q3).
5. Add the missing tests (below) and rerun.
6. Write ADR-201, ADR-202, ADR-203.

**Questions and what each outcome means**

| Q | Test | Pass | Fail → action |
|---|---|---|---|
| Q1 history fidelity | `test_q1_history_fidelity` (two renderings) | at least one rendering is accepted and used | both fail → **stop, re-plan** (Option A fork or TUI gateway) |
| Q2 statelessness | `test_q2_no_hidden_history_between_runs` + container-recreate variant | no leakage between runs without session key | leakage → wipe `HERMES_HOME` per run, or **re-plan** |
| Q3 events | capture files | documented schema from real streams | missing fields → poll `GET /v1/runs/{id}`, mark served model unknown |
| Q4 stop and retention | `test_stop_settles_as_cancelled` + new retention probe | stop latency and retention window measured | long retention unknown → shorter consumer reconnect deadline |
| Q5 tool lockdown | config yields only `mcp__paw__*`; **gateway-side canary call** at readiness (model listing is unreliable) | canary seen by the gateway | no canary → do not admit runs |
| Q6 MCP | stub MCP server, streamable HTTP + static bearer; down at start and mid-run | **done**, see s0-findings | n/a |
| Q7 instructions | `test_q7_instructions_are_followed` | followed | not followed → leading user-context turn instead |
| Q8 hook block | guard plugin | **done**: block works, raise fails **closed**, silent | hook must report to the gateway (002D) |
| Q9 Hermès approvals (new) | `trust: untrusted` run | **done**: only with `trust: untrusted`; deny or stop exits work | adapter treats `waiting_for_approval` as a fault |
| Q10 Idempotency-Key (new) | replay with PAW run id as key | same run returned, header present | not reliable → keep spec rule: dispatch_unknown = INTERRUPTED |
| Q11 metrics off (new) | confirm no outbound traffic besides the model API | none | add to egress allow-list denial |

**Status 2026-11-02: Q1-Q4, Q6-Q10 done; Q5 probe and Q11 open (s0-findings.md).**

**Exit gate:** contract suite green against the pinned image; ADR-201 (session authority), ADR-202 (tool boundary), ADR-203 (OpenAI provider) written; any spec deviation recorded in the spec. **Needs from you:** the S0 environment file with the OpenAI key (kept outside the repo).

## 3. 002A: runtime bridge and run lifecycle (M)

**Goal:** start, stream, cancel and account for Admin Hermès runs; every event durable before the browser sees it.

**Depends on:** S0 pass (and its captured event schema). **Migration 0006** (0005 is auth sessions from 001D-06; later phases shift by one: 002B 0007, 002C 0008, 002D 0009, 002F 0011).

**Data**
- `agent_runtimes(id, actor_id, hermes_version, image_digest, status, last_health_at, reported_hashes)` (one row, concurrency 1)
- `agent_run_leases(runtime_id PK, run_id, acquired_at, heartbeat_at)`
- `agent_runs(id, session_id, actor_id, runtime_id, client_request_id, state, profile_version, compiled_hash, assembly_hash, hermes_run_id, requested_model, served_model, tokens_in, tokens_out, cache_read, cache_write, cost_usd, rate_card_version, error_code, queued_at, started_at, finished_at)` with `UNIQUE (session_id, client_request_id)` and a partial unique index allowing one non-terminal run per session
- `agent_run_events(run_id, seq, type, payload jsonb, source_event_id, occurred_at)` PK `(run_id, seq)`, unique `(run_id, source_event_id)`, append-only
- `ops_rate_cards(version, model, input_per_mtok, output_per_mtok, effective_from)`
- Minimal `agent_sessions` here (id, owner, workspace, title, status) so a run has a home; the full ledger is 002B

**Code** (`src/paw_server/modules/runtime/`)
- `protocol.py`: `AgentRuntime` protocol (`create_run, stream_events, get_run, stop_run, capabilities, health`)
- `hermes_adapter.py`: HTTP client, SSE reader with reconnect then polling, event normaliser (built from the S0 captures)
- `lifecycle.py`: state machine from spec section 9 (invariants enforced in SQL, not only code)
- `lease.py`: lease acquire/heartbeat/release (reuse the fencing idea from `ingestion/jobs.py`)
- `accounting.py`: rate-card cost, budget and timeout enforcement
- `agent_worker.py` (new entrypoint): FIFO queue consumer, concurrency 1
- `api/agent.py`: `POST /v1/agent-sessions/{id}/runs`, `GET /v1/agent-runs/{id}`, `GET /v1/agent-runs/{id}/events` (SSE, `Last-Event-ID` replay, polls the table at about 100 ms or uses LISTEN/NOTIFY), `POST /v1/agent-runs/{id}/cancel`, `GET /v1/runtime/health`
- Config: `PAW_HERMES_URL`, `PAW_HERMES_KEY`, `PAW_AGENT_QUEUE_LIMIT` (5), `PAW_AGENT_STOP_DEADLINE_S`, per-run and daily budgets
- Compose: `hermes-runtime` service (private network, no published port) and `agent-worker` in dev and home compose

**PR sequence**
1. Migration 0005 + schema tests (partial unique index, append-only, unique keys)
2. `AgentRuntime` protocol + fake-backed adapter + normaliser (against fake first, then S0 captures)
3. Lifecycle state machine + unit tests for every transition and invariant
4. Admission endpoint (idempotency, 409, 429) + queue + lease
5. Event consumer: commit event, then fan out; reconnect then poll
6. Browser SSE with replay
7. Cancel, stop deadline, INTERRUPTED
8. Accounting (usage, rate card, budget, timeout)
9. Compose services + readiness (`/ready` 503 on missing capabilities) + docs

**Tests (spec → test)**

| Req / test | Check |
|---|---|
| 002A-R01 | external port scan, 401 without key, no key in image/config |
| 002A-R02 | fake without a capability → `/ready` 503 |
| 002A-R03, T203 | concurrent duplicate submit = one run; second non-terminal run = 409; queue overflow = 429 |
| 002A-R04 | kill stream mid-run; sequence has no gaps or duplicates vs a direct capture |
| 002A-R05, T202 | Playwright disconnect and reconnect at mobile width, compare event ids |
| 002A-R06 | fake that ignores stop; hard kill during cancel → INTERRUPTED |
| 002A-R07, T206 | usage fixture, slow fake run, cost arithmetic unit tests |
| T204 | kill runtime mid-run → INTERRUPTED, partial output kept, never SUCCEEDED |

**Acceptance:** send a message, watch it stream, navigate away and return to the finished answer; duplicates never duplicate runs or events; killing the runtime mid-run shows INTERRUPTED with partial output.
**Web:** minimal chat page (message box, streamed text, status, cancel) on the existing shell. **Open:** LISTEN/NOTIFY vs polling for SSE, decided in PR 6 by measuring the 300 ms relay target.

## 4. 002B: session ledger, rehydration and recovery (M)

**Goal:** PostgreSQL alone restores any conversation; Hermès state is disposable. **Depends on:** 002A. **Migration 0006.**

**Data:** `agent_session_messages(id, session_id, seq, kind, role, body | content_ref, run_id, tool_invocation_id, redaction_state)` unique `(session_id, seq)`, append-only via grants/triggers; `agent_summaries(id, session_id, from_seq, to_seq, model, prompt_version, message_id)` non-overlapping active ranges; session fork columns (`parent_session_id, fork_seq, last_seq`).

**Code** (`modules/sessions/`): ledger append (transactional with run admission), `assembler.py` (deterministic render, `assembly_hash`; budgets from spec 12.4; renders history in the format S0 Q1 proved), summariser job, tool-result stubbing (>2,000 tokens), fork, soft delete + purge, boot reconciler (runs, leases, operations).

**PRs:** ledger + append-only guarantees; assembler + property tests; admission writes the user message and run in one transaction; terminal run appends assistant/tool messages; reconciler at agent-worker boot; summariser; fork; delete/purge.

**Tests:** 002B-R01 concurrent append + redaction tombstone; R02 property test over 200 generated sessions (byte-identical output); R03/T205 cold start with Hermès container recreated (needs real image); R04 kill both containers mid-run and mid-operation; R05 long-session fixture; R06 fork-and-diverge.
**Acceptance:** conversation continues after both containers are recreated; a 200-turn session stays within budget and keeps facts named in its summary; no run is SUCCEEDED without its terminal event.
**Risk:** history re-sent each run raises cost; measure cost per task, keep the stable prefix first for provider prompt caching.

## 5. 002C: identity, ActorProfile and compiler (S)

**Depends on:** 002A. **Migration 0007.** Tables: `actors`, `actor_profile_versions` (immutable spec, one active per actor via partial unique index), `actor_compiled_profiles`.
**Code** (`modules/actors/`): JSON Schema for ActorProfile v1, compiler (SOUL.md, config fragment, gateway policy rows, constraints text, `compiled_hash`), activation with confirmation token for tool/budget changes, restart-between-runs, runtime hash report; web profile view with diff.
**Tests:** R01 update-after-create rejected and partial unique index; R02 golden files (deterministic); R03 edit SOUL.md claiming a permission, gateway still denies; R04/T211 tamper with the mounted file → `/ready` 503; R05 agent-initiated activation denied and in-flight run keeps its version.
**Acceptance:** edit persona, activate N+1, next run uses it, inspector shows each earlier run's version. **Needs:** the real config key names from S0 (compiler output must match what Hermès reads).

## 6. 002D: MCP gateway and policy (M) → **M1**

**Depends on:** 002A, 002C. **Migration 0008.** Tables: `gateway_tools(name, version, side_effect_class, schema_json, enabled)`, `gateway_policy_grants`, `gateway_tool_invocations`.
**Code** (`modules/gateway/`): MCP server (streamable HTTP, internal listener only, separate process/port, Python MCP SDK chosen after S0 Q6), runtime credential auth, tool registry, **policy engine in the spec 10 order** (first failure denies; reason codes), output shaping (16 KB bound, cursors, untrusted-content envelope), tools `paw_system_status`, `paw_run_status`, `memory_search`, `memory_propose` (tables arrive in 002E; stubs return empty until then), `docs_search` and `docs_get_source` wired to the **existing** retrieval service, startup lockdown check, backup `pre_tool_call` hook plugin.
**Tests:** R01 not reachable via the public proxy, 401 for missing/rotated credential (T220 later); R02/T210 `NO_ACTIVE_RUN`; R03 decision-table tests for every step, audit row count equals call count; R04/T209 forged identity fields rejected by schema; R05 oversize and injection fixtures; R06/T207 extra toolset enabled → `/ready` 503; T208 "run a shell command" has no tool and the attempt is visible.
**M1 acceptance (spec 2):** chat with Admin Hermès, tool call visible in inspector and PostgreSQL, containers destroyed and recreated with state wiped and the conversation continues, out-of-set tool use fails closed and is audited. **The phone-over-HTTPS part needs 001D-04 and 001D-06.**

## 7. 002E: memory v1 and context assembly (M)

**Depends on:** 002B, 002D. **Migration 0009.** Tables `memory_items` (embedding pinned to the existing `text-embedding-3-small`, 1536 dims, `tsv`, GIN + HNSW), `memory_sources`, `memory_run_reads`.
**Code** (`modules/memory/`): service (SQL-side scoping by actor, workspace, status before ranking; deterministic tiebreak), per-run injection (context block 3), tools, episode-summary job after terminal runs, candidate review/promote/supersede/retract API, web memory view. Reserved categories rejected.
**Tests:** R01 source required, deleting a source flags the item; R02 propose then search → candidate absent until promoted; R03/T213 cross-workspace and retracted items with guessed ids; R04 reserved category rejected; R05 `run_memory_reads` agrees with the inspector. **Live:** recall and hygiene evals (section 11).
**Acceptance:** a decision saved days earlier is recalled with its source in a fresh session after full restart; a proposed memory waits and is unused until promoted.

## 8. 002F: approvals and side-effect safety (M)

**Depends on:** 002D, **and real owner login with a step-up window (001D-06)**. **Migration 0010.** Tables `approval_requests`, `approval_operations`.
**Code** (`modules/approvals/`): canonicalise args and `args_hash`; request bound to actor, tool, version, args_hash, resource, connection, expiry (24 h); single-use; executor runs from **stored** canonical arguments with `op_key = hash(approval_id)`; outcome SUCCEEDED/FAILED/UNKNOWN; reconciler searches the provider for the marker before any retry; platform-event ledger message; optional continuation run (O3 default yes); inbox API and web page with exact payload.
**Tests:** R01/T216 mutated args, expired, reused → denied; R02 model text after approval cannot change payload; R03/T218 fake GitHub times out after creating the issue → exactly one issue; R04/T217 restart with pending approval, revoke while pending; R05 stale session prompts re-authentication.
**Blocker to state plainly:** until 001D-06, the step-up check can only be a stub; 002F is **not accepted** without it.

## 9. 002G: GitHub connector (M)

**Depends on:** 002D, 002F. **Migration 0011.** Tables `connector_connections`, `connector_secrets` (ciphertext, `key_id`; key held outside the DB).
**Code** (`modules/connectors/github/`): App auth and per-call installation tokens with expiry-aware cache (confined to the connector module), install flow with state validation, uninstall webhook, rate-limit handling, six read tools, `github_create_issue` (external write, sandbox repo, `<!-- paw-op:{id} -->` marker).
**Tests:** R01 tampered/replayed state rejected; R02 secret-pattern scan over all persisted rows and logs in CI (T219); R03/T215 guessed repo → `OUT_OF_SCOPE`; R04 mocked 403/429 with rate-limit headers; R05 revoke between two calls.
**Needs from you:** register a GitHub App (selected repos, contents, pull requests, checks, issues permissions) and one disposable sandbox repository; the app private key is delivered as a secret, never committed.

## 10. 002H: evaluation, observability, release (M) → **M2**

**Depends on:** all. **Migration 0012** (`ops_audit_events`, trace ids).
**Code:** OpenTelemetry through request, run, tool, executor and provider calls (trace id on every row); run inspector (rebuildable from PostgreSQL alone); log redaction and CI log scan (no bodies, payloads or secrets); `evals/` with `contract/` (fake Hermès, must be 100%) and `tasks/`+`graders/`+`runner.py` (live, three trials, pass^3); nightly and on-demand run storing results per Hermès version, model and profile version; runbooks (runtime crash, stuck run, lost stream, provider outage, GitHub revocation, cost cap, Hermès upgrade, credential rotation, restore).
**Gates:** deterministic suites 100% (policy decision tables, recovery, approval binding and exactly-once, secret scan zero findings, contract tests); live pass^3 thresholds per spec 15 (tool selection and grounding 0.80, memory recall 0.70, hygiene 0.90, GitHub reading 0.80, approval honesty 0.90, injection refusal 0.90). Start with 20 tasks; changing a threshold needs an ADR.
**Cost control:** the live suite spends money; run it on demand and nightly on the owner's laptop or a protected CI job, never on untrusted PRs.

## 11. Cross-cutting work

| Item | Plan |
|---|---|
| **CI** (`.github/workflows/server.yml`) | Add: contract tests against the fake on every PR (already fast); integration job with pgvector service; secret scan; a protected job for the real-Hermès contract suite with a low-limit key (main only) |
| **Egress allow-list** | Docker has no native egress allow-list. Plan: put `hermes-runtime` on an internal network plus a forward proxy that allows only `api.openai.com` and `paw-server`; verify with a negative test. Decide the proxy (squid/tinyproxy) in 002A |
| **Two listeners** | Public API :8000 (behind the proxy) and internal MCP :8001 (never routed by `deploy/proxy/nginx.conf`; assert in an exposure test, D-T06) |
| **Role separation** | Run migrations and the app with different DB roles (001D-02/06); gateway tables read-only for the app role where the spec requires |
| **Compose** | Add `hermes-runtime` and `agent-worker` to `deploy/compose.home.yaml` and the dev compose; Hermès image digest in the release manifest (001D-05) |
| **Web** | Chat (002A), inspector (002H), approvals (002F), memory (002E), profile (002C), connections (002G); all usable at 360 px with keyboard and screen reader (spec 20) |
| **Docs** | OpenAPI, MCP tool contracts, data-model diagram, runbooks, environment reference updated per phase |

## 12. Dependencies and blockers summary

| Blocker | Affects | Owner |
|---|---|---|
| Real Hermès image + OpenAI key | S0 and everything after | built by Bharat; key in an env file outside the repo |
| Dedicated OpenAI project key with a monthly budget | cost safety from 002A | Bharat |
| 001D-06 real login + step-up | 002F acceptance, M1/M2 sign-off | not built |
| 001D-04 remote access over HTTPS | M1/M2 phone demos | not built |
| GitHub App + sandbox repo | 002G | Bharat |
| Decisions O1 to O5 (plan.md section 3) | O3 and O4 before 002F and 002A | Bharat |

## 13. Definition of done for the story (spec 23)

All substories merged with linked tests; deterministic gates 100%; live gates met and recorded with Hermès version, model and profile version; ADR-201 and ADR-202 accepted; M1 and M2 demonstrated from a phone on an outside network with restart steps recorded; OpenAPI, MCP contracts, data model diagram, runbooks and environment reference current; a second actor can be added by configuration only (Story 4 handoff).
