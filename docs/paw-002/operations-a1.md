# Operating cost controls (A1)

Every model call an actor makes passes the PAW model proxy. The proxy reserves the call's worst-case cost before the provider is contacted, then settles it from the provider's reported usage. The provider key lives only in the PAW gateway process. There is no direct-provider fallback: if the proxy is down or refuses, the call does not happen.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `PAW_MODEL_PROXY_TOKEN` | empty (proxy off) | The runtime's credential for the proxy. At least 24 characters and different from `PAW_GATEWAY_TOKEN`: a tool credential cannot spend, and a spend credential cannot call tools. |
| `PAW_MODEL_PROXY_UPSTREAM_KEY` | empty | The provider key. Required when the proxy is on. Give it to the gateway process only, never to Hermès. |
| `PAW_COST_SHARED_DAILY_USD` | 3 | All actors together, per Asia/Kolkata day. Server configuration, not a profile setting. |
| `PAW_MODEL_PROXY_UPSTREAM_BASE` | OpenAI `/v1` | Provider endpoint. |
| `PAW_MODEL_PROXY_TIMEOUT_S`, `_MAX_BODY_BYTES`, `_DEFAULT_OUTPUT_TOKENS` | 120, 4 MiB, 4096 | Bounds on one call. |

Per-run and per-actor daily limits are in the actor's profile (defaults $0.50 per run, $3 per actor per day). Run time is the profile's `max_run_seconds`.

`./dev.sh` generates `~/.config/paw/model-proxy.token`, keeps the provider key on the host for the gateway, and gives the Hermès container only the proxy token and URL. The container refuses to start with a provider key next to the proxy.

## What is counted

- Run model calls, and conversation summaries. Independent document chat and embeddings are not.
- **Confirmed** is settled cost from provider usage at the rate card in force when the call was authorised.
- **Held** is the reservation for calls not yet settled. It counts against every limit.
- **Unconfirmed** is the part of the held amount for calls that may or may not have been billed (broken stream, missing usage, timeout, crash). It stays against the day it was authorised on. Missing usage is never treated as zero.

Unknown prices, unsupported payloads, a missing or stale run identity, and exhausted budgets are refused before any provider call.

## Readiness

- Gateway: `GET /_health` is liveness. `GET /_ready` is 200 only when enforcement can work, otherwise 503 with the failing check names (`model_proxy_not_configured`, `upstream_key`, `profile`, `budget`, `pricing`, `tokenizer`, `database`). It never returns values or keys.
- API: `GET /v1/runtime/health` adds `enforcement_<check>` (or `enforcement_proxy_unreachable`) to `missing` and reports `ready: false` whenever `PAW_MODEL_PROXY_TOKEN` is set and the gateway says it cannot enforce. The Admin Hermès page shows this as "not ready".
- Start-up: the gateway refuses to start (exit 2) if a token encoding for a priced model is unavailable. `tiktoken` may download an encoding on first use, so run the gateway once with network access before restricting egress.

## Reading spend

`GET /v1/agent-spend` (signed in, owner workspace only) returns today's confirmed, held and unconfirmed amounts for Admin Hermès and for all agents, with the limits and the reset time. Every run response carries a `cost` block; a run with no recorded attempt is `unknown`, not zero. The Admin Hermès page shows both. Responses hold amounts only, never conversation text.

## Time limits

A run's deadline is set when it starts and stored, so a restart cannot extend it. Past it, new model and tool calls are refused, the runtime is asked to stop, and the run ends cancelled, or interrupted with its partial output if the stop is not confirmed within `PAW_AGENT_STOP_DEADLINE_S`. Its error code is `deadline_exceeded`.

## Summaries and the daily reset

A summary reserves its cost like a run call. If the day is spent it is not sent: the job waits until the next Asia/Kolkata midnight (`error_code='budget_deferred'`) and does not use up its retries. Recent history keeps being used meanwhile.

## After a crash

The agent worker's reaper runs a reconciliation pass every few seconds:

- A hold that was never sent (older than 60 s) is released. The send fence makes this safe: a late sender is refused.
- A send with no outcome (older than 5 minutes) is labelled uncertain. It keeps counting.
- A finished run with exactly one uncertain attempt is settled from the runtime's remaining usage, only if nothing else of that run is in flight, the numbers are consistent and no cache fields make them ambiguous. Anything less clear stays uncertain until the day ends.

Watch the logs for `cost reconciliation` and `late_evidence settled`. Neither logs content.

## Rolling out a change

Runtime configuration (model, proxy URL or token, tool list) is applied by recreating the Hermès container, so drain first:

1. Stop sending new messages (or accept that queued runs wait).
2. Wait until no run is `QUEUED`, `DISPATCHING`, `RUNNING` or `CANCELLING`:
   `SELECT id, state FROM agent_runs WHERE state IN ('QUEUED','DISPATCHING','RUNNING','CANCELLING');`
3. Deploy and migrate, then restart the gateway and the agent worker. Check `GET /_ready` on the gateway is 200.
4. Recreate Hermès (`./dev.sh`, or your supervisor's equivalent). `GET /v1/runtime/health` must report `ready: true`, with no `config_*` or `enforcement_*` entries, before you send traffic.

Runs that were already in flight before this feature shipped have no stored deadline and are not time-limited; draining first avoids that.

Never run Hermès with a provider key, and never start it before the gateway: it parks a model or tool server that is down at its start.

## Acceptance evidence

`tests/costs` covers atomic reservation and settlement, the proxy's denials (zero provider calls), streams, identity and logs; `tests/costs/test_reconcile.py` covers crashes and late evidence; `tests/costs/test_acceptance.py` covers readiness and the midnight and cross-midnight behaviour; `tests/gateway` covers tool budgets and deadlines; `tests/sessions/test_summaries_recovery.py` covers summary budgets and deferral. The pinned-runtime contract (`PAW_PROXY_CONTRACT=1 pytest tests/costs/test_hermes_proxy_contract.py`) runs the shipped plugin against a fake provider with no real key.
