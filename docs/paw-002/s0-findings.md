# PAW-002 spike S0: findings against the real Hermès

Run date: 2026-11-02 (container clock 2026-11-01 UTC). Evidence: `paw-server/tests/contract/hermes/` and
`docs/paw-002/captures/` (raw responses; scanned, no secrets).
Runtime under test: **Hermes Agent v0.22.5+3239.gc7da41d** (`main` commit `c7da41dd522d`, unreleased), image `paw-hermes:s0`,
provider `openai-api`, model `gpt-4o-mini`, interim OpenAI key (see plan.md section 3).

**Status: S0 core gates (Q1, Q2) pass, with design consequences. S0 is NOT complete:** Q5 (runtime tool-list probe),
Q6 (MCP), Q8 (hook), Q9 (Hermès approvals with MCP) and Q11 (outbound traffic) are still open. ADRs below are **drafts**.
Do not start production code until the open items are closed or consciously deferred by the owner.

## 1. Result summary

| Q | Result | Evidence |
|---|---|---|
| Q1 history fidelity | **Pass via fallback.** Compact-text rendering of prior turns is used by the model. OpenAI-style `role: tool` messages / `tool_calls` are **not** used | `test_q1_compact_text_history_is_used` passes; `test_q1_openai_style_tool_messages_are_used` is a strict `xfail` |
| Q2 statelessness | **Pass only with memory off.** With defaults, a fact told in run 1 reached run 2 through Hermès's native memory tool (`memories/MEMORY.md`), not through session history (each run's `session_id` equals its own run id). With `memory.memory_enabled=false` and `memory.user_profile_enabled=false`, the stale file stayed on disk and was not used | `test_q2_no_hidden_history_between_runs` |
| Q3 event schema | Documented from real streams (section 2) | captures `*events.json` |
| Q4 stop | Stop returns `stopping` (or `cancelled` if the run has not started); settles `cancelled` in **0.51 s**; event `run.cancelled` with `turn_exit_reason`. Terminal-run retention is **at least 4.8 minutes** (upper bound not measured) | `test_stop_settles_as_cancelled`, manual probe |
| Q5 tool lockdown | **Mechanism works, probe open.** All ~14 native toolsets are on by default for the API platform. Disabling them for `api_server` removes the shell (T208 behaviour) and cuts input tokens from ~10,400 to ~620 per run | `test_native_shell_is_unavailable_and_prompt_is_small`; a runtime-side way for PAW to read the effective tool list is **not found yet** |
| Q6 MCP | **Not tested** (needs a stub MCP server) | |
| Q7 instructions | Pass: `instructions` content is followed | `test_q7_instructions_are_followed` |
| Q8 hook block | **Not tested** | |
| Q9 Hermès approvals | **Not tested with MCP.** With all native toolsets off there is nothing to approve; the MCP trust-gate path (spec risk) is unverified | |
| Q10 Idempotency-Key | Pass: replay returns the same run, `replayed: true` in the body; the capabilities list advertises `runs_idempotency: {durable: true, retention_seconds: 86400}` | `test_idempotency_key_replays_the_same_run` |
| Q11 metrics / egress | Shared metrics are **off by default** (config comment). Outbound traffic **not measured** | |

Real run: **16 passed, 1 xfailed**. Fake-backed suite: **60 passed, 5 skipped**.

## 2. Wire facts the adapter must follow (observed, not documented)

- `POST /v1/runs` → `202 {"run_id","status":"started","replayed":false}`. Stop → `{"run_id","status":"stopping","admission_id"}`.
- `GET /v1/runs/{id}` → `object: hermes.run`, `status`, `session_id` (= run id by default), `admission_id`, `execution_generation`,
  `pending_controls`, `output`, `usage {input_tokens, output_tokens, total_tokens}` (no cache fields seen), `runtime {provider:"", model:"", route_source:"global"}`.
- **The served model is not reported** (`runtime.model` and `model` are empty/`hermes-agent`). 002A-R07 must record "unknown" as the spec's fallback allows, or take it from the configured model.
- **SSE frames are unnamed**: `data: {"event":"message.delta","run_id":...,"seq":0,"delta":"Hi",...}`. The event type is inside the JSON, a `: open` comment starts the stream, and `seq` is contiguous from 0 per run, so PAW can dedup on `(run_id, seq)`.
- Terminal events seen: `run.completed` (with `output`, `usage`, `completed`, `partial`, `interrupted`), `run.cancelled`.
- Capabilities (real) include `run_approval_response`, `approval_events`, `session_resources`, `session_chat`, `session_fork`, `run_steer`, `runtime.tool_execution: "server"`. The listener exposes far more than PAW needs; it must stay private.
- Startup warning: "API server is network-accessible AND the terminal backend is 'local' (unsandboxed)". Another reason lockdown is mandatory.

## 3. Consequences for the design (drafts of the ADRs)

**ADR-201 (session authority) draft: confirm D202 (PAW ledger, stateless rehydration), with two amendments.**
1. History is rendered as compact text turns (the spec's Q1 fallback), not OpenAI tool messages. Tool exchanges in history become text such as `[tool X returned ...]`. Assembler tests must pin this format.
2. Statelessness holds **only if** Hermès native memory and profile memory are off. Add to the compiled profile and to readiness (002C-R04): `memory.memory_enabled=false`, `memory.user_profile_enabled=false`.
3. Proposal to evaluate in 002A: send the PAW run id as `Idempotency-Key`. Hermès durably dedups for 24 h, so a `DISPATCHING`/dispatch-unknown run can be recovered by safe replay instead of always becoming `INTERRUPTED`.

**ADR-202 (tool boundary) draft: D205 confirmed as necessary; D206 untested.**
- Native toolsets must be disabled for the `api_server` platform by explicit list. The wrapper image does this at every start (`PAW_HERMES_LOCKDOWN=1`, default) and fails the start if any toolset remains enabled. This parses CLI output (`hermes tools list`), which is brittle; the 002C compiler should write the config directly.
- Open: a runtime-side readiness probe (Q5) and the MCP server behaviour (Q6/Q8/Q9).

**ADR-203 (OpenAI as provider, replaces OpenRouter for now)**: provider key `model.provider: openai-api`, `model.default: gpt-4o-mini`, key `OPENAI_API_KEY` in `HERMES_HOME/.env`. Works. Cost backstop is the OpenAI project budget (no per-key hard cap by default). Use a dedicated project key before any real use.

**Cost note (new):** unlocked, every run sent ~10.4k input tokens of tool schemas; locked it is ~0.6k. With MCP tools added the prompt will grow again; measure in 002D. Budget accounting must use the reported `usage`.

## 4. Open items to close S0 (in order)

1. **Q6 + Q9 + Q8**: build a stub MCP server (streamable HTTP, static bearer), register it in the Hermès config, and test: tool listing and call, gateway down at start and mid-run, whether the MCP trust gate emits `approval.request` / `waiting_for_approval`, and `pre_tool_call` blocking.
2. **Q5 probe**: find or build a way for PAW to read the effective tool list or a config hash at readiness (candidate: a read-only sidecar that reports hashes of `config.yaml`, `SOUL.md`, and the tools list).
3. **Q11**: confirm the only outbound destination is `api.openai.com` (packet or proxy log).
4. **Q4**: measure the true retention window of terminal runs (poll an old run id over time).
5. **T205-style recreate test** against real Hermès: destroy and recreate the container (state wiped) and rerun the history test. (The suite already ran on a freshly recreated container once.)
6. Convert the drafts above into ADRs and update the spec with the deviations.

## 5. Reproduce

```
# image (installer pinned by sha256, Hermes pinned to a commit)
docker build -t paw-hermes:s0 \
  --build-arg INSTALL_SH_SHA256=abf338605dae6dd28d101673ac6aed7995c955745db46d47676bc591a7957209 \
  --build-arg HERMES_COMMIT=c7da41dd522dc19cc08bbfcfa69d4b078224b45d deploy/hermes
# env file OUTSIDE the repo: API_SERVER_KEY (>=32 chars), OPENAI_API_KEY,
#   HERMES_CONFIG_SET="model.provider=openai-api;model.default=gpt-4o-mini"
docker compose --env-file /path/s0.env -f deploy/hermes/compose.s0.yaml up -d
cd paw-server && PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY=<API_SERVER_KEY> \
  PAW_HERMES_CAPTURE_DIR=../docs/paw-002/captures uv run pytest tests/contract/hermes
```
Costs a few cents of OpenAI usage per run. Never run on untrusted pull requests.
