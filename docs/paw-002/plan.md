# PAW-002 development plan and S0 status

Spec: `PAW_002_Admin_Hermes_Harness_Requirements_v2.docx` (v2.0, 2026-10-29). Branch: `paw-002-admin-hermes`.
Per-phase engineering plans: [phase-plans.md](phase-plans.md).
Audience: Bharat (owner) and whoever runs the spike. Written 2026-11-02.

## 1. Where we are

**S0 is nearly done** against the real pinned Hermès (see [s0-findings.md](s0-findings.md)): Q1, Q2, Q3, Q4, Q6, Q7, Q8, Q9 and
Q10 are answered. Q1 and Q2 (the spec's stop-and-re-plan gates) **pass**, each with a design consequence. Still open: Q5
(a deterministic readiness probe) and Q11 (outbound traffic). No production code (`src/`) has been written.

| Item | State |
|---|---|
| Contract suite `paw-server/tests/contract/hermes/` | **19 pass + 1 expected failure against the real image** (stable over two runs); the fake-backed part runs in CI |
| Stub MCP server + guard plugin | Built (`tests/contract/hermes/stub_mcp_server.py`, `deploy/hermes/plugins/paw-guard`) |
| Wrapper image `deploy/hermes/` | Applies at every start: native-tool lockdown, memory off, guard plugin, MCP `resources/prompts/sampling/elicitation` off |
| Production code (`src/`) | None, deliberately |
| ADR-201/202/203 | Drafts in s0-findings.md section 4, awaiting owner acceptance |

**Spec corrections found by S0** (the spec should be amended): tool names are `mcp__paw__<tool>` not `mcp_paw_`; a failing
`pre_tool_call` hook fails **closed**, not open; Hermès's own memory leaks state across runs unless disabled; Hermès health
cannot see a parked MCP server so readiness needs a gateway-side canary; model text is never evidence a tool ran.

## 2. What the Hermès docs check (2026-11-02) changed

The spec's section 5 says "every row must be re-proven against the pinned image". Re-reading the docs shows where it is
thin, and a few things the spec does not account for.

| Finding | Consequence |
|---|---|
| **No official Docker image or Dockerfile is documented.** Install is `curl -fsSL .../install.sh \| bash` | We must build a wrapper image ourselves. Pin the exact version and verify the script/checksum; do not run an unpinned remote script in a build |
| `conversation_history` message format is **not documented** (no role names, tool-call or tool-result shape) | Q1 is the critical test. The harness tries OpenAI-style tool messages and a compact-text rendering; the spec's fallback chain stays valid |
| `POST /v1/runs` body is only partly documented; the exact input field name is not stated in one of the pages (the other says `input`) | Contract test asserts `input`; any 4xx is a finding |
| SSE event list is **incomplete** (documented: `message.delta`, `message.interim`, `tool.started`, `tool.completed`, `run.completed`, `run.failed`, `run.cancelled`, `run.interrupted`, `approval.request`, `run.steered`, subagent events). No `run.started`; no confirmed model/id fields | Q3 capture must record every real frame before the normalizer is designed |
| **Hermès has its own approval flow**: status `waiting_for_approval`, event `approval.request`, `POST /v1/runs/{id}/approval` (e.g. MCP trust-gate consent) | **Not in the spec.** A run can stall waiting on a prompt PAW does not know about. Needs a decision in S0: configure it off, or have the adapter deny and fail the run with a typed error. Add as Q9 |
| Hermès has **`unknown` status, `/resolve-unknown`, `execution_generation`** for restart recovery | Overlaps our `INTERRUPTED` reconciler. S0 should decide whether PAW calls `resolve-unknown` or treats `unknown` as `INTERRUPTED` (the spec's rule) |
| `POST /v1/runs` supports an **`Idempotency-Key`** header (replay returns the same run, HTTP 202, `Idempotency-Replayed: true`, kept 24 h) | Could make `DISPATCHING / dispatch_unknown` recoverable by safe replay with the PAW run id as key, instead of always marking `INTERRUPTED`. Proposal for ADR-201, to be verified (Q10). Fits the spec's "never replay": a replay returns the same run, it does not start a second one |
| Terminal status retention is "brief", duration not stated | Q4 measures it |
| Usage has `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`; `runtime` has provider and model | Served model may be available, so the "unknown" fallback in 002A-R07 may not be needed |
| `HERMES_HOME` is not mentioned; `~/.hermes` is the implied default | The spec's `HERMES_HOME` volume design needs confirming on the real image |
| Requires a model with at least 64k context | Constrains O4 (model choice) |
| Other endpoints exist (`/steer`, `/resolve-unknown`, `/v1/chat/completions`, `/v1/responses`) | The runtime listener must stay private; none of these may be reachable from outside paw-server |

## 3. Inputs for S0

**Decision (2026-11-02, owner): use the existing OpenAI key for now instead of OpenRouter.** This deviates from spec D211
("proposed"), so record it as ADR-203 once S0 confirms it works:

| Spec (OpenRouter) | With OpenAI direct |
|---|---|
| Hermès provider: OpenRouter | `openai-api` (key `OPENAI_API_KEY` in `HERMES_HOME/.env`; exact `config.yaml` keys to be proven in S0) |
| Egress allow-list: `openrouter.ai` | `api.openai.com` |
| Hard credit limit on the key is the final cost backstop | OpenAI has no per-key hard cap by default: set a **monthly budget limit on the OpenAI project** and use a **separate project key for Hermès**, not the PAW embeddings key. Until then the only cap is the prepaid balance |
| Model choice | any OpenAI chat model with at least 64k context (gpt-4o-mini has 128k) |

Interim state: S0 may reuse the key in `paw-server/.env` on the owner's laptop only. Do not copy it into CI or the home install;
create a dedicated project key first.

Still needed from the owner: a model choice if not `gpt-4o-mini` (O4), and an OpenAI project budget limit.

**Install safety (done 2026-11-02):** the vendor installer was downloaded and read before use. It clones
`github.com/NousResearch/hermes-agent`, fetches a pinned `uv`, installs dependencies, writes only a local install receipt
(it sends nothing), and supports `--commit`, `--hermes-home` and `--non-interactive`. Hermès itself has a separate "shared
metrics" feature that reports a receipt on a later start when collection is on: **disable it and verify (Q11)**.
The wrapper image `deploy/hermes/` verifies the script by sha256 and pins Hermès to an exact commit.
**Pin correction (2026-11-02):** the newest release tag (`v2026.9.24`) does not contain the `pm/` directory that the current
installer requires, so the first build failed. The image is pinned to `main` commit
`c7da41dd522dc19cc08bbfcfa69d4b078224b45d` (2026-11-01), which has it. This means S0 runs an **unreleased** Hermès commit;
revisit when a tagged release includes `pm/`. The first clone from this network took about 5 minutes and one attempt hit a
transient "RPC failed" error, so re-running a failed build is reasonable.

## 4. Deviations to decide before 002A (proposed, not yet made)

The spec assumes a greenfield data layout. The repo already has conventions, so these are real choices:

| Spec says | Repo does | Recommendation |
|---|---|---|
| ULIDs for all ids | UUIDs | Keep UUIDs; record as ADR (time-ordered UUIDv7 if ordering is needed) |
| One PostgreSQL schema per module | Single schema, raw SQL via psycopg, Alembic migrations | Keep the existing layout; prefix table names by module (`agent_`, `memory_`, ...) |
| Roles `paw_migrate` / `paw_app` | One role | Do with 001D-02/06 role separation, not inside 002 |
| `/v1/agent-sessions...` | `/v1/chat` already exists for RAG Q&A | Keep both: `/v1/chat` stays the document Q&A; agent routes are separate |

## 5. Dependencies on 001D (not complete)

| 002 needs | From | State |
|---|---|---|
| Owner session with a step-up window (002F-R05) | 001D-06 real authentication | **Not built.** Today there is only a long-lived bearer token. 002F cannot be accepted without it |
| HTTPS phone demo of M1/M2 | 001D-04 remote access | Not built (home compose and proxy exist) |
| Off-host backup | 001D-02 | Built, local stand-in only |
| Private network, egress allow-list | 001D-01 compose | Network split exists; an egress allow-list does not |

So M1 can be built and tested locally, but cannot be *accepted* (spec section 2) until 001D-04 and 001D-06 land.

## 6. Order of work

1. **S0** (needs section 3): build the wrapper image under `deploy/hermes/`, run
   `PAW_HERMES_URL=... PAW_HERMES_KEY=... PAW_HERMES_CAPTURE_DIR=... uv run pytest tests/contract/hermes -m hermes_real`,
   add Q5/Q6/Q8/Q9/Q10 tests (need container config and a stub MCP server), then write ADR-201 and ADR-202.
   **If Q1 or Q2 fail, stop and re-plan** (spec rule).
2. 002A runtime bridge, then 002B ledger, 002C profile, 002D MCP gateway, **M1**.
3. 002E memory, 002F approvals, 002G GitHub, 002H eval and release, **M2**.
Each substory as small PRs with CI green, per spec section 16.

## 7. Running the contract tests

```
cd paw-server
uv run pytest tests/contract/hermes                      # fake: harness + documented protocol
PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY=... \
PAW_HERMES_CAPTURE_DIR=../docs/paw-002/captures \
uv run pytest tests/contract/hermes                      # real: also runs hermes_real tests, saves raw captures
```
Never run the real suite on untrusted pull requests; it spends money.

## 8. Not covered by the tests yet

Q5 (effective tool list equals the approved list), Q6 (MCP streamable HTTP with static bearer; gateway down at start and
mid-run), Q8 (`pre_tool_call` block and fail-open), Q9 and Q10 above, the `state.db` wipe variant of Q2. These need the
real container and the PAW gateway.
