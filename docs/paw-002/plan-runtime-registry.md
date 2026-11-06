# Runtime registry slice

6 November 2026. Implemented and verified locally. Implements the slice approved in chat, within the accepted
[actor foundation](plan-actor-foundation.md). GitHub follows this slice.

## Contract

Persist runtime → actor, endpoint and outbound secret reference, desired profile,
desired running/stopped state, and observed readiness. Store only hashes of inbound
gateway and model credentials; keep their purposes separate. Registration cannot
rebind an existing runtime to another actor. Revocation survives process restarts.

Each worker is bound to one registry entry. Several workers can run concurrently;
queue claims select only that runtime's actor. PostgreSQL enforces one executing
run per actor and correct runtime/actor binding. Recovery and deadlines use the
same runtime as dispatch. No supervisor or container creation in this slice.

Gateway HTTP authentication resolves the credential to a runtime on every request.
Tool calls, model calls and reports use that binding, never caller-supplied actor
or runtime identifiers. Reports and model run IDs cannot cross runtime boundaries.
Unknown, revoked, stopped or disabled identities fail closed. Admin uses the same
registry with an idempotent bootstrap from existing configuration; bootstrap must
not resurrect revoked credentials. API cancellation and privacy stops route by the
stored run runtime, including when Hermes IDs coincide across containers.

## Build and evidence plan

1. Add migration and registry/credential service, local operator provisioning.
2. Filter and serialize queue claims; bind workers and outbound adapters to registry.
3. Bind gateway, proxy, reports and cancellation to authenticated/stored runtime.
4. Test two actors and runtimes, overlapping dispatch, actor concurrency, rotation,
   revocation after restart, malicious run IDs/reports, and purpose separation.
5. Run existing Admin, gateway, cost and ledger/recovery regressions, lint and types.

Use disposable local databases and fake runtimes/providers; no paid API calls.
Existing model proxy reservation and settlement rules stay authoritative. Runtime
credentials grant no owner API access. Registration is local operator work until
actor creation/approval and supervision are implemented later.

## Implementation coverage

| Requirement | Implementation and acceptance evidence |
|---|---|
| Runtime actor, endpoint/secret reference, desired profile/state and observed boot | Migration `0015`, `modules/runtime/registry.py`, local `runtime_registry` command; boot/profile and endpoint acceptance tests |
| Hashed, separate gateway/model credentials; rotation and persistent revocation | Credential table and immutability trigger; bootstrap, purpose, rotation, stop/restart and captured-principal tests |
| Route each actor to its runtime; overlap actors without overlapping one actor's runs | Actor-filtered queue claims, actor lock, composite foreign keys and unique executing-actor index; concurrent two-worker and direct database constraint tests |
| Bind tools, spending and reports to authenticated identity | Gateway/proxy resolve the credential's runtime; cross-run model denials and report isolation tests; both actors' spending rows checked |
| Bind MCP session traffic to the credential | SDK authenticated principal carries credential/runtime IDs, no bearer value; HTTP session reuse and SSE replay tests |
| Preserve cancellation, privacy, deadlines and restart recovery | Stored-runtime routing; secondary cancellation/forget/deadline/recovery tests; report cleanup test with colliding Hermès IDs |
| Preserve Admin and existing data | Idempotent Admin bootstrap, upgrade-from-0014 acceptance, existing Admin/gateway/cost/ledger regressions |

Operating commands, rollout and limitations are in
[runtime registry operations](operations-runtime-registry.md). Existing owner
APIs and the web UI continue to default to Admin; the owner session API can also
select an existing registered actor. Container supervision, actor creation and
delegation remain outside this slice.

## Verification results (6 November 2026)

- Full deterministic server regression: **930 passed, 2 skipped, 26 deselected**
  in 563.51 seconds. Two warnings arose in ingestion tests (Pydantic field alias
  metadata and connection-pool cleanup). Live/provider and real-Hermès gates were
  excluded; optional checks were not counted as passes.
- After the final MCP session binding and privacy cleanup changes: **36 passed**
  across registry acceptance, real MCP HTTP and privacy regressions.
- After the final claim-versus-stop lock: **64 passed** across all **17 registry
  acceptance tests** and the Admin lifecycle suite. The stop test holds an
  uncommitted registry update while another connection attempts a claim.
- Server lint, formatting and strict type checks passed (106 source files).
- Web checks: **66 passed**, type checking and production build passed. Web lint
  completed with warnings in existing components; this slice changed no web code.
- Operator command help, local documentation links and diff whitespace checked.

These results cover local development with disposable PostgreSQL databases and
fake runtimes/providers, plus real MCP HTTP transport. No paid model calls or
generalist benchmark runs were made. The migration was tested on disposable
databases; activation on the normal installation still requires the documented
migration and service restart. No commit or deployment was made for this slice.
