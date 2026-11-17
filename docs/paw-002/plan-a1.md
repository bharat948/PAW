# A1: cost controls and runtime limits

Approved 2026-11-04. Implementation branch: `paw-002a-cost-limits`, based on the completed 002B ledger.

Every actor model call must pass an authenticated PAW model proxy, reserve its maximum bounded cost before forwarding, and settle once against a versioned rate card. The provider key stays in PAW. Unknown prices, unsupported payloads, missing identity and exhausted budgets fail closed. No automatic direct-provider fallback.

Defaults: $0.50/run, $3/actor/day, $3 shared/day; Asia/Kolkata midnight. Preserve existing profile limits. Include conversation summaries; exclude independent document-chat and embedding workloads. Uncertain requests retain their reservation against the original day; never claim missing usage means zero cost. The UI distinguishes confirmed and reserved/unconfirmed spending.

Run deadlines persist from execution start and survive recovery. Expiry denies new model/tool calls, requests cancellation, and preserves partial output. Unconfirmed cancellation ends interrupted. Summary jobs defer when daily budgets are exhausted.

## Atomic build sequence

1. A1-01 pinned Hermès/fake-provider compatibility gate: endpoint, auth, streaming, tools, usage, immutable run attribution; denied calls never reach upstream.
2. A1-02 additive pricing/attempt/reservation schema and exact decimal calculations.
3. A1-03 atomic reserve, settle, release and uncertainty service; concurrent and replay tests.
4. A1-04 authenticated model proxy and bounded request enforcement.
5. A1-05 runtime configuration, identity binding and removal of provider keys from Hermès.
6. A1-06 authoritative gateway budget checks.
7. A1-07 budgeted summaries and reset deferral.
8. A1-08 durable execution deadlines and cancellation.
9. A1-09 crash accounting and late-evidence reconciliation.
10. A1-10 owner-scoped spending reads and chat presentation.
11. A1-11 integrated acceptance, readiness and operational guidance.

Each task is a separate reviewable commit. Runtime compatibility failure blocks dependent integration. No actor creation, memory, approvals, delegation, new budget settings page, or full home topology in this phase. The shared cap is server configuration; actor limits remain in Profile.

## Acceptance

Denied requests produce zero provider calls even without tools. Concurrency cannot overspend a shared reservation balance. Test retries, duplicate settlement, interrupted streams, missing usage, midnight and cross-midnight requests, crash/restart, summaries discarded after privacy changes, unknown rates and stale run identity. Verify no request bodies or credentials in logs. Readiness fails if configured enforcement is unavailable. Rollout drains active runs before replacing runtime configuration; no fail-open fallback.

## Status

A1-01 to A1-11 are implemented on `paw-002a-cost-limits`, one commit each. Operating guidance, readiness and the acceptance map are in [operations-a1.md](operations-a1.md). Not covered here: the real-provider click-through, outbound network enforcement and the home topology, which remain later work.
