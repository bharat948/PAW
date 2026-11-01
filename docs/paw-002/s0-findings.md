# PAW-002 spike S0: findings against the real Hermès

Run date: 2026-11-02 (container clock 2026-11-01 UTC). Evidence: `paw-server/tests/contract/hermes/` and
`docs/paw-002/captures/` (raw responses; scanned, no secrets). Reproduce with section 6.
Runtime under test: **Hermes Agent v0.22.5+3239.gc7da41d** (`main` commit `c7da41dd522d`, unreleased), image `paw-hermes:s0`,
provider `openai-api`, model `gpt-4o-mini`, interim OpenAI key (plan.md section 3), MCP SDK 2.3.0 (client and stub).

**Status: Q1, Q2, Q3, Q4, Q6, Q7, Q8, Q9, Q10 answered. Q5 (deterministic readiness probe) and Q11 (outbound traffic) remain open.**
Neither blocks the spec's stop-and-re-plan rule (that is Q1 and Q2, both pass). The ADRs below are **drafts** until the owner
accepts them; production code (002A) may start once the two open items are either closed or consciously deferred.

## 1. Result summary

| Q | Result | Evidence |
|---|---|---|
| Q1 history | **Pass via the spec's fallback.** Prior turns rendered as compact text are used. OpenAI-style `role: tool` / `tool_calls` history is **ignored** | `test_q1_compact_text_history_is_used`; strict `xfail` for the tool-message form |
| Q2 statelessness | **Pass only with native memory off.** Default Hermès leaks facts between runs through its own memory tool (`memories/MEMORY.md`), not session history (`session_id` = run id). With `memory.memory_enabled=false` and `memory.user_profile_enabled=false` it does not, even with the stale file on disk | `test_q2_no_hidden_history_between_runs` |
| Q3 events | Documented from real streams (section 2). Served model is **not** reported | captures |
| Q4 stop / retention | Stop: `stopping` then `cancelled` in **0.51 s** (also cancels a run waiting for approval in ~1 s). Retention of finished runs: **at least 4.8 min**, upper bound not measured | `test_stop_settles_as_cancelled`, probes |
| Q5 tool lockdown | Mechanism works (shell gone, prompt ~10.4k to ~0.6k tokens). **A deterministic runtime probe is still open**; asking the model to list tools is unreliable (it sometimes refuses) | `test_native_shell_is_unavailable_and_prompt_is_small` |
| Q6 MCP | **Works** with a static bearer over streamable HTTP. Details in section 3 | `test_q6_*` |
| Q7 instructions | Pass | `test_q7_instructions_are_followed` |
| Q8 hook | `pre_tool_call` block **works**; a raising or timed-out hook **fails closed** (the spec assumed fail-open); blocked calls emit **no event** | manual runs, section 3 |
| Q9 Hermès approvals | The stall is real only with `trust: untrusted`; default `trust: full` never prompts. Exits proven (section 3) | manual runs |
| Q10 idempotency | Pass; capabilities advertise `runs_idempotency {durable: true, retention_seconds: 86400}` | `test_idempotency_key_replays_the_same_run` |
| Q11 outbound traffic | Shared metrics are off by default (config comment). **Traffic not measured** | open |

Real Hermès: **19 passed, 1 expected failure**, stable over two consecutive runs. Fake-backed suite: 60 passed, 8 skipped.

## 2. Wire facts the adapter must follow (observed, not documented)

- `POST /v1/runs` returns `202 {"run_id","status":"started","replayed":false}`; stop returns `{"run_id","status":"stopping","admission_id"}`.
- `GET /v1/runs/{id}`: `status`, `session_id` (= run id), `admission_id`, `execution_generation`, `pending_controls`, `output`,
  `usage {input_tokens, output_tokens, total_tokens}` (no cache fields), `runtime {provider:"", model:"", route_source:"global"}`.
  **Served model is not reported**: 002A-R07 records the configured model and "unknown" for served.
- **SSE frames are unnamed**: `data: {"event":"message.delta","run_id":...,"seq":0,...}`; a `: open` comment first; `seq` is contiguous from 0, so PAW dedups on `(run_id, seq)`.
- Terminal events: `run.completed` (`output`, `usage`, `completed`, `partial`, `interrupted`), `run.cancelled` (`turn_exit_reason`).
  Tool events: `tool.started {tool, preview}`, `tool.completed {tool, duration, error}`. Approval: `approval.request {prompt_id, description}`.
- **Tool names are `mcp__<server>__<tool>`** (double underscores), e.g. `mcp__paw__paw_echo`. The spec's `mcp_paw_` prefix is wrong for this build.
- `status: waiting_for_approval` carries `pending_controls: [{kind:"approval", prompt_id, execution_generation, command, description, choices:[once,deny,session,always]}]`;
  resolve with `POST /v1/runs/{id}/approval {request_id: <prompt_id>, execution_generation, choice}` -> `{"status":"resolved"}`.
- Capabilities (real) also advertise `run_steer`, `session_chat`, `session_fork`, `session_resources`, `runtime.tool_execution: "server"`. The listener exposes much more than PAW needs and must stay private.

## 3. MCP, hook and approval behaviour (Q6, Q8, Q9)

| Scenario | Observed |
|---|---|
| Server up, tool call | `tool.started` then `tool.completed {error:false}`; the server log shows the call; the model reports the real result |
| Bearer token | Server returns 401 without it (stub test); Hermès sends the configured header |
| `tools.include` | Removes unlisted tools of our server (a write tool left out of the list was never callable) **but leaves four Hermès utility tools**: `list_resources`, `read_resource`, `list_prompts`, `get_prompt`. `tools.resources: false` and `tools.prompts: false` remove them |
| Sampling, elicitation | **On by default** per server (the server could ask Hermès to run model calls, or pause a run for input). Disable both explicitly |
| Server **down at Hermès start** | Hermès API comes up healthy. The MCP server is **parked after 3 failed attempts and never retried** until Hermès restarts. The model then **claims it ran the tool when no tool ran** (no tool events, the server saw nothing) |
| Server drops **mid tool call** | `tool.completed {error:true}` within about a second; the model reports the error; the run status is still `completed`. Hermès **reconnects by itself** on the next call once the server is back |
| `trust: full` (default) | Write-capable tools run with no approval |
| `trust: untrusted` | A write-capable tool (no `readOnlyHint: true`) makes the run **stall** in `waiting_for_approval`; the tool is **not** executed. `stop` cancels it in ~1 s; `deny` through `/approval` lets the run finish with the tool never executed. A mistyped trust value is treated as `untrusted` |
| Hook, block mode | Native shell blocked (marker file never written); baseline without the guard wrote it |
| Hook, raising | Logged as `Hook 'pre_tool_call' callback _guard raised`; the call is **blocked** (fail closed), PAW tools too |
| Hook, blocked call | **No `tool.*` event** is emitted, only the log line and the model's prose |
| Hook, allowed call | Hook receives `mcp__paw__paw_echo`, the same name as in events |
| Unlocked + MCP | With native toolsets on, the model did not use the PAW tool at all (it tried `manage_connections` instead). Not explained; irrelevant while locked, but unlocked runs are not a valid test bed for MCP |

## 4. Consequences for the design (drafts)

**ADR-201 (session authority): confirm D202 with amendments**
1. Render prior turns as compact text, not OpenAI tool messages; tool exchanges become text such as `[tool X returned ...]`; the assembler tests pin this format.
2. Statelessness requires Hermès native memory off; add both `memory.*` switches to the compiled profile and to readiness (002C-R04).
3. Evaluate in 002A: send the PAW run id as `Idempotency-Key` so a dispatch-unknown run can be recovered by safe replay (Hermès dedups durably for 24 h).

**ADR-202 (tool boundary): D205 confirmed necessary; D206 still to prove in 002D**
- Disable all native toolsets for the `api_server` platform (done by the image at every start) **and** disable MCP `resources`, `prompts`, `sampling` and `elicitation` for the PAW server; set `trust: full` **explicitly** (typo means untrusted means stalls).
- **The guard hook is useful but is not an audit source**: it fails closed (good) but is silent. 002D must make it report blocked attempts to the PAW gateway. The prefix is `mcp__paw__`.
- **Readiness cannot rely on Hermès's own health.** A parked MCP server is invisible to `/v1/capabilities`. Required: (a) start Hermès only after the PAW gateway is healthy; (b) a **canary tool call** at readiness and after any restart, verified on the **gateway side** (the gateway counts it), and restart Hermès if it fails; (c) the gateway's own health feeds `/ready`.
- **Evidence rule:** model text is never evidence that a tool ran or succeeded. The ledger takes tool results from `tool.started/completed` events cross-checked with gateway audit rows; evals grade on audit rows, not prose. A run with status `completed` can contain failed tool calls.
- **Adapter rule for `waiting_for_approval`:** treat as a fault. Deny via `/approval` (or stop) and fail the run with a typed error. With `trust: full` it should never occur, so it also indicates mis-configuration and fails readiness.
- Approvals for external writes stay entirely inside PAW (D207): the gateway returns `APPROVAL_REQUIRED`; Hermès's own approval path is not used.

**ADR-203 (OpenAI replaces OpenRouter for now)**: `model.provider: openai-api`, `model.default: gpt-4o-mini`, `OPENAI_API_KEY` in `HERMES_HOME/.env`. Works. Cost backstop is an OpenAI project budget; use a dedicated project key before real use. Owner decision: OpenRouter may be added later, so provider and model stay profile settings and the egress allow-list is per provider (see plan.md section 3).

**Cost note:** unlocked, every run sent about 10.4k input tokens of native tool schemas; locked about 0.6k; locked with two MCP tools about 2.3k. Budget accounting must use the reported `usage`.

## 5. Open items

1. **Q5 probe:** design the canary (section 4) and test it, including the "server down at start" case. Optionally a read-only sidecar reporting hashes of `config.yaml`, `SOUL.md`, and the MCP tool list for 002C-R04.
2. **Q11:** confirm the only outbound destination is `api.openai.com` (proxy log or packet capture).
3. **Q4:** measure the upper bound of terminal-run retention.
4. **Recreate test (T205)** with real conversation history through the 002B assembler (needs 002B).
5. Owner decisions: accept ADR-201/202/203 drafts and the model/key/budget points in plan.md.

## 6. Reproduce

```
docker build -t paw-hermes:s0 \
  --build-arg INSTALL_SH_SHA256=abf338605dae6dd28d101673ac6aed7995c955745db46d47676bc591a7957209 \
  --build-arg HERMES_COMMIT=c7da41dd522dc19cc08bbfcfa69d4b078224b45d deploy/hermes
# env file OUTSIDE the repo: API_SERVER_KEY (>=32 chars), OPENAI_API_KEY, STUB_TOKEN,
#   HERMES_CONFIG_SET="model.provider=openai-api;model.default=gpt-4o-mini",
#   PAW_MCP_URL=http://stub-mcp:8001/mcp, PAW_MCP_INCLUDE="paw_echo, paw_write_note, paw_slow"
docker compose --env-file /path/s0.env -f deploy/hermes/compose.s0.yaml up -d
cd paw-server && PAW_STUB_URL=http://127.0.0.1:8001 PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY=<API_SERVER_KEY> \
  PAW_HERMES_CAPTURE_DIR=../docs/paw-002/captures uv run pytest tests/contract/hermes
```
Image defaults: native-tool lockdown on, memory off, guard plugin on, MCP resources/prompts/sampling/elicitation off, `trust: full`.
Knobs for experiments only: `PAW_HERMES_LOCKDOWN=0`, `PAW_HERMES_GUARD=0`, `PAW_GUARD_MODE=off|raise`, `PAW_MCP_TRUST=untrusted`.
Costs a few cents of OpenAI usage per run. Never run on untrusted pull requests.
