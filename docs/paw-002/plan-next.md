# Plan: finishing Story 2, and what comes after

Updated 2026-11-04 for Bharat. 002B is implemented on `paw-002b-ledger`, with local 002B acceptance complete. [Coverage and evidence](implementation-002b.md). Other phase descriptions below retain their earlier baseline unless updated.

## 0. Where we are

| Area | State | Evidence |
|---|---|---|
| S0 spike, ADRs | done | `s0-findings.md`, ADR-201 to 204 accepted |
| 002A runtime bridge | steps 1 to 7 of 9 | chat works end to end against the real Hermès; cancel, stop deadline, restart recovery |
| 002B durable conversations | implemented; local 002B acceptance complete | task-aware compaction, PostgreSQL context, recovery, privacy and browser gates; `implementation-002b.md` |
| 002C identity | done | Profile page, versions, compiler, per-run profile binding |
| 002D gateway | PRs 1 to 8, home compose missing | real tools (docs, calendar, status), audit, attestation, fail closed; verified on the real container |
| Calendar connector + Integrations page | built, **never run against real Google** | needs your click-through |
| 001D-01, 02, 06 | done | home env contracts, encrypted backup and restore drill, Google login |
| 001D-03, 04, 05, 07 to 10 | not built | host install, remote access, releases, launcher, ops, cost |
| Tests | server 740 passed, web 60 passed | opt-in real-container tests also pass |

Spec milestones:
- **M1 (first governed loop)** = S0 + 002A to 002D. We have most of it. The 002B cold-restart gate now passes. Remaining: cost limits, the home topology, and the hosted phone demo over HTTPS.
- **M2 (Story 2 release)** = 002E to 002H: memory, approvals, GitHub, evaluation. None started.

## 1. What is left in Story 2, in three tracks

### Track A: close M1 (about 4 groups of work)

**A1. Cost, limits and the served model (002A steps 8 and part of 9)**
- Rate cards and per-run cost from token usage; per-run and daily budgets enforced in the gateway (the policy engine already has the BUDGET_EXCEEDED step and the `ops_rate_cards` table exists).
- Run timeout from the profile's `max_run_seconds` (today only `max_turns` is applied).
- The model Hermès actually used, from `post_api_request` (today we only know the configured one).
- Close the two open spike questions: Q11 (no outbound traffic except OpenAI and the gateway) and Q4 (how long Hermès keeps finished runs).
- Visible: a cost line per run in the chat and a daily total; a run stops with a clear reason when the cap is hit.

**A2. 002B: the session ledger, rehydration and recovery**
- Implemented, including task-checkpoint compaction with a size fallback. Recovery, privacy and local browser acceptance pass. Local 002B acceptance is complete.
- See [requirement coverage and tests](implementation-002b.md) and [operations](operations-002b.md). Hosted phone acceptance remains A4 after deployment.

**A3. Home topology (rest of 002A step 9, 002D PR 8)**
- Add Hermès, the gateway and the agent worker to the home compose, on a private network with no published ports.
- An egress proxy so the Hermès container can only reach OpenAI and the gateway (verified with a negative test).
- Exposure test: the gateway and Hermès are unreachable through the public proxy.
- `/ready` merges runtime health, config match and gateway reachability.
- Image digest in the release manifest.

**A4. Remote access and the phone demo (001D-04, and the M1 acceptance)**
- Needs a decision from you (section 4): how the phone reaches your laptop over HTTPS.
- Then the recorded M1 demonstration from a phone on an outside network.

### Track B: memory and approvals

**002E memory v1** (about 6 PRs): memory items with provenance, candidate then active lifecycle, scoped hybrid search, per-run injection into the context, tools `memory_search` and `memory_propose`, an episode summary after each run, a memory page to review, promote and retract. Acceptance: a decision saved days ago is recalled with its source in a fresh session after a full restart; a proposed memory is unused until you promote it.

**002F approvals** (about 6 PRs): any action that changes something outside PAW waits for you. The request is bound to the exact arguments, expires in 24 hours, can be used once, and PAW (not the model) executes it from the stored arguments, exactly once, even across a restart. Needs the step-up sign-in that already exists. Web inbox shows the exact payload. This is also what makes ticket cancel and project create safe in Story 3.

### Track C: GitHub and release

**002G GitHub connector** (about 5 PRs): GitHub App install flow, read tools (repositories, files, pull requests, check runs, issues), and one gated write (create an issue in a sandbox repository). Needs your GitHub App and a throwaway repository.

**002H evaluation and release** (about 6 PRs): OpenTelemetry traces, a run inspector rebuilt from PostgreSQL alone, log redaction with a CI scan for secrets, the evaluation suite (deterministic gates at 100 percent; live tasks run three times each with the spec's pass thresholds), runbooks, and the M2 phone demonstration.

## 2. Recommended order

```
A1 cost+limits ─► A2 ledger+T205 ─► A3 home topology ─► A4 phone demo = M1
                         │
                         ├─► 002E memory ─► 002F approvals ─► 002G GitHub ─► 002H eval = M2
                         └─► Story 3 (003A to 003E) can start here, in parallel
```

Why this order:
1. **A1 first** because it is the safety net: nothing should run longer or cost more than you decided before we add more tools that spend money.
2. **A2 next** because memory (002E) writes into the ledger and approvals (002F) append to it. Building them on the current stopgap would mean rewriting them.
3. **A3 and A4** can overlap with 002E once A2 lands, but the M1 sign-off needs both.
4. **002F before 002G** because the GitHub write tool is the first thing that needs an approval.

Size, relative to what we have done: A1 is about one phase of the 002C size; A2 is about the size of 002D PRs 1 to 4; A3 is smaller but fiddly (networking); 002E, 002F and 002H are each about the size of 002C plus 002D PR 4; 002G is a bit smaller. I will not promise calendar dates: the long poles are your external setup (remote access, GitHub App) and the live evaluation runs, not the code.

## 3. When Stories 3, 4 and 5 can start

I have read Story 3's spec in full (`PAW_003_Independent_Ticket_Service_Requirements_v2.docx`). I have **not** read Stories 4 and 5 (they do not exist as documents in the repo yet), so what I say about them comes from references in the Story 2 and 3 specs.

| Story | What it is | Can start when | Depends on from Story 2 |
|---|---|---|---|
| **3 Tickets** | independent ticket service: workflow engine, evidence-gated completion, board, Hermès tools | its first milestone (003A to 003E, human ticketing) can start **after A2**, in parallel with 002E to 002H | actors table (done), the SSE stream (done), nothing else for 003A to 003E |
| 3, Hermès part (003F, 003G) | Hermès plans and manages tickets | after 002F | gateway (done) and approvals (002F) |
| **4 Actor factory** | a second agent by configuration only | after Story 2 is done | gateway, profiles, memory and approvals accepting a second actor with configuration only |
| **5 Dispatch and sandboxes** | agents pick up tickets, several runtimes, sandboxed shell and file tools | after Stories 3 and 4 | per-run credentials, multi-runtime concurrency, sandboxed tools |

My suggestion: finish Track A, then run **002E to 002H and Story 3 M1 in parallel** if you want a visible new product surface sooner (the board), or keep to one thing at a time if you prefer a calmer review load. Say which.

## 4. What I need from you

| Item | Needed for | Notes |
|---|---|---|
| **Check what is already built** (30 minutes): restart `./dev.sh`; connect Google Calendar and ask the Agent about your week; edit the Profile and activate; restart Hermès | confidence before we build on it | I have only seen these work against fakes and the real container, never in your browser with real Google |
| Remote access choice | A4, M1 demo | Tailscale (private, simplest, free), Cloudflare Tunnel (public HTTPS name, needs a domain), or router port-forward (not recommended) |
| OpenAI project key with a monthly budget, used only by Hermès | A1 | the last cost backstop; today Hermès uses your general key |
| GitHub App and one disposable sandbox repository | 002G | I will give exact registration steps when we get there |
| Decision O3: after you approve an action, should Hermès continue the conversation automatically? | 002F | default is yes |
| Decision O4: the model | A1 | today `gpt-4o-mini`; the spec needs at least 64k context; OpenRouter later |
| Publish the Google OAuth app (even unverified, single user) | Calendar | removes the weekly "needs reconnecting" in Testing mode |
| Process: keep pushing straight to `main` as today, or one branch and pull request per phase | all | PRs give you a review point and CI; direct pushes are faster |

## 5. Housekeeping I have noted

- The dev gateway listens on all interfaces so the container can reach it; the token is the only protection. Home uses a private network (A3).
- The first Admin Hermès profile on your dev database predates the "continue long result" tool; it needs one new profile version to get it.
- `./dev.sh` recreates the Hermès container on every start. That is right for applying a profile, but means a chat in flight dies on restart (it ends INTERRUPTED, as designed).
- CI exists (`server.yml`, `web.yml`) but does not run the gateway or agent tests against a pgvector service with the migrations through 0010; check and extend in A3.
- Accessibility at 360 px and keyboard use is specified for every page (spec 20); the Agent, Profile and Integrations pages have not had that audit yet. Schedule it into 002H.
- A short ADR for "persona sent per run, not in `SOUL.md`" is recorded in `plan-002c-002d.md`; move it into the ADR list when 002H collects them.

## 6. Risks

| Risk | Mitigation |
|---|---|
| History re-sent every run raises cost as conversations grow | stable prefix first for provider caching, summaries and tool-result stubbing in A2, per-task cost measured in A1 |
| Hermès is an unreleased build, pinned to one commit | contract tests against the real image on every change of the pin; the adapter is the only code that knows its wire format |
| Live evaluations cost money and vary by model | run on demand and nightly on your machine only, three trials each, thresholds fixed in an ADR |
| Remote access exposes your laptop | private network (Tailscale) or tunnel with login in front; Google login and step-up already protect the app; the gateway never leaves the private network |
| Review load: 37 commits landed on `main` in one push | smaller, per-phase pull requests from here, if you prefer |
