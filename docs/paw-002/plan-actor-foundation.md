# Design: the Admin Hermès foundation for many actors

Written 2026-11-04 for Bharat. Status: **all decisions accepted 2026-11-04** (section 7). The 002B build plan is in `plan-002b.md`.

## 0. What you asked for

Admin Hermès should be able to bring a new Hermès actor into existence from a contract, so that later that actor can
take tickets (Story 3), start cleanly with its contract, and have everything it does recorded. You want the
foundation built for that now, not retrofitted.

**Short answer:** yes, and the cheap moment to do it is now, while the ledger (002B) is still unbuilt. One change to
the idea: Hermès should not control Docker itself. It asks PAW for a new actor; PAW checks the contract and you
approve it; a small, separate supervisor starts the container. Section 2 explains why.

## 1. The model: five parts

```
            contract (versioned, immutable)          supervisor (only part that touches Docker)
 actor ──── profile v1, v2, ... ──── compiled ────►  starts one container per actor, from the pinned image,
   │                                                 with that actor's compiled settings and its own credential
   │                                                              │
   │                                                    runtime instance (container)
   │                                                              │ tools only through
   ▼                                                              ▼
 ledger: every message, tool call, result, decision,    PAW gateway: credential ─► runtime ─► actor ─► run
 delegation and approval, tagged with the actor              policy from that run's contract version
```

| Part | What it is | Exists today? |
|---|---|---|
| **Actor** | an identity: you (`owner`), `admin-hermes`, later e.g. `research-hermes` | yes (table from 002C) |
| **Contract** | the actor's versioned profile: persona, model, tools, budgets, scopes, plus (new) runtime limits and who it reports to | yes for one actor; needs the runtime section and multi-actor rules |
| **Runtime instance** | one Hermès container bound to one actor and one contract version, with its own credential | one hard-coded runtime only |
| **Supervisor** | starts, stops and recreates containers to match what PAW says should be running | no (today you restart with `./dev.sh`) |
| **Ledger** | the permanent record of every interaction, by every actor | no (that is 002B) |

## 2. How "Admin creates an actor" works, and why Hermès must not hold Docker

**Why not let the Hermès container run Docker?** A process that can start containers can start one that mounts your
whole disk, so it can read your keys and change anything on the laptop. Hermès reads untrusted text all day (calendar
titles, documents, later web pages and tickets). One successful prompt injection would then own your machine. The
spec's own rule, "Hermès has exactly one way to act on the world: the PAW gateway", exists for this reason.

**The flow instead:**

1. You ask Admin Hermès, for example, "set up a research assistant that can only search my documents".
2. Admin calls a gateway tool `actor_propose` with a contract: name, persona, model, tools, budgets.
3. PAW validates it. A proposed actor can never get more than its creator has: tools must be a subset of Admin's
   grants, budgets at most Admin's, no permission to create further actors. Anything else is rejected before you see it.
4. The proposal appears in your approvals inbox (and on the Profile page) with the exact contract. You approve with a
   recent Google sign-in (step-up, already built).
5. PAW creates the actor, compiles its contract, issues it its own gateway credential, and records "should be running".
6. Approval registers the actor as **available** (approved, not running). Its **first assignment** makes the supervisor
   start a container from the same pinned image with that actor's settings, and the container must prove (attestation,
   already built) that it runs exactly the approved tools and model. A contract change for a sleeping actor only moves
   its desired version; the next start uses it. The Actors page distinguishes approved, starting, ready and sleeping.
7. The actor is now ready. Admin can delegate to it ("ask research-hermes to find..."), and later Story 3 can assign it
   tickets. Every step above is a ledger entry.

**The supervisor** is the only component with Docker access, and it is deliberately dumb:
- it can only start the one pinned Hermès image, with no host folders mounted, not privileged, with memory and CPU
  limits, on the private network behind the egress proxy;
- it works from a table of desired state written by PAW, not from requests by any agent;
- it talks to Docker through a socket proxy that allows only container create, start, stop, inspect and remove for
  containers carrying the PAW label;
- it also fixes today's "restart required" chore: when you activate a new contract, it recreates that actor's
  container between runs, automatically.

This replaces decision D-b ("PAW never holds Docker access, restart by hand"). D-b was right for one container
restarted rarely; it does not work for actors created on request. Section 7 asks you to confirm.

## 3. What this changes in 002B (the ledger)

The ledger is designed for many actors from the first migration:

| Ledger need | Design |
|---|---|
| Who said or did it | every row carries `actor_id`: you, Admin, any sub-actor, or `system` |
| Who a conversation is with | a session has an owner (who asked: you, or Admin when it delegates) and an agent actor (who answers) |
| Delegation is visible end to end | a session started by an actor records `parent_session_id`, `parent_run_id` and the tool call that created it, so "you → Admin → research-hermes" is one traceable tree |
| Which contract produced an answer | every run already records its contract version and hash (002C); ledger rows link to the run |
| Actor lifecycle is recorded | platform events: actor proposed, approved, created, started, contract changed, stopped, credential rotated |
| Message kinds | user, assistant, tool_call, tool_result, platform_event, summary, plus `delegation` (an actor handing work to another) |
| Same history rules for every actor | one assembler; budgets and summary rules come from the actor's contract |

The rest of 002B is unchanged: append-only rows, deterministic briefing (assembler), summaries for long chats, stubs
for long tool results, startup reconciler, fork, delete, and the wipe-the-container test.

## 4. New phase: actor runtime foundation (pulled forward from Stories 4 and 5)

The minimum of Stories 4 and 5 that the foundation needs, and nothing more:

1. **Contract v2:** add the runtime section (resources, `always_on` or `on_demand`, max concurrent runs = 1), a
   `reports_to` actor, and a `may_propose_actors` flag that only Admin has. Golden-file tests as in 002C.
2. **Per-runtime credentials in the gateway:** each runtime has its own token (stored hashed), so the gateway
   resolves credential → runtime → actor → run. Today there is one shared token and one runtime name.
3. **Runtime registry with desired state:** which actors should be running, on which contract version, and their
   observed state from the supervisor and from attestation.
4. **Supervisor service:** the Docker reconciler described in section 2, with the socket proxy; dev and home compose.
5. **Multi-runtime dispatch:** the worker routes each queued run to the runtime of that session's actor (today any
   queued run goes to the one runtime); one run at a time per runtime, several runtimes in parallel.
6. **Delegation:** a gateway tool for Admin to hand a task to another actor and wait for or check the result; recorded
   as a child session.
7. **`actor_propose` and the approval:** uses 002F's approval machinery; until 002F lands, the proposal can be approved
   on the Profile page with step-up.
8. **Web:** an Actors page (list, contract, status, start/stop, history), and delegation shown in the chat.

What stays in Stories 4 and 5: actors editing their own contracts, deeper hierarchies (sub-actors creating actors),
dispatch from tickets, sandboxed shell and file tools, per-run credentials.

## 5. Revised order

```
A1 cost and limits ─► 002B ledger (multi-actor) ─► actor runtime foundation ─► 002F approvals + actor_propose
                                                                             ─► 002E memory (per actor)
                                                                             ─► home topology, 002G, 002H
Story 3 M1 can start after the ledger and the runtime registry.
```

Cost and limits come first, more than before: several actors mean several times the spend. Budgets become per actor
plus a global daily cap.

## 6. Safety limits from day one

- At most N actors running at once (default 3 on a laptop: each container is about 1 GB of image and a few hundred
  MB of memory), and a global daily spend cap above every actor's own cap.
- A proposed contract can never exceed its creator's tools or budgets; only Admin may propose; only you approve.
- Each actor has its own credential; stopping an actor revokes it.
- Sub-actors cannot create actors (depth 1) until Story 4 decides otherwise.
- Every actor's container passes attestation before its first tool call (already built), so a container that drifts
  from its contract is refused.

## 7. Decisions (accepted 2026-11-04)

| # | Decision |
|---|---|
| F1 | A separate supervisor with restricted Docker access starts containers; never Hermès itself (replaces D-b) |
| F2 | Admin proposes, the owner approves with a recent sign-in |
| F3 | On demand: approval makes an actor available; its first assignment starts the container; stopped after 15 idle minutes; Admin always on |
| F4 | At most 3 running actor containers in total, including Admin, one active run per actor. Configurable; excess work queues. A starting ceiling until RAM is measured on the home laptop |
| L1 | Conversation list: top-level chats, newest by last activity, first-message title (renamable), actor name beside the title |
| L2 | Partial answers: exact text kept and marked unfinished in the UI and in the next turn's context; whether something happened is decided by the recorded tool outcome, never by the answer's wording |
| L3 | Tool results: kept with timestamps and scope checks; above about 2,000 tokens a short summary plus a reference; full result for authorised viewers; changing facts (tomorrow's calendar) are fetched again when freshness matters |
| L4 | Summaries: from about 24,000 recent-turn tokens, or sooner if the model's context is smaller; `gpt-4o-mini`, configurable; shown with the messages they cover; charged to the conversation's actor budget |
| L5 | Delete: hidden at once, live data purged after 30 days. Forget this message: excluded from future context at once, including summaries and derived memories, invalidated before the next run. Backup expiry disclosed (7 daily + 4 weekly copies: up to about 35 days); deletions re-applied on restore. Provider requests already sent cannot be recalled |
| L6 | Retention: until deleted; delegated chats follow the same rule; deleting a parent states explicitly whether its delegated chats go too |
| L7 | Editing: fork at the edited message; the original branch and its evidence stay; the new branch starts with the replacement text; a fork never replays a tool call or carries over an approval |
| L8 | Cost: no per-conversation cap. Per-run ceiling, per-actor daily limit, one global daily limit; budget reserved atomically before dispatch so two workers cannot both spend the last of it; a parent task shows its total cost including children |
| L9 | Delegated chats nest under the parent: actor, task, status, cost, link. A blocked child that needs the owner also appears in the attention inbox |
| L10 | Owner sees all activity across actors, including actor-to-actor chats, tool calls, results and decisions; credentials redacted. That visibility never gives actors access to each other's history |

**Schema rule from these decisions:** keep three roles apart everywhere. The **data owner** (the workspace owner, you),
the **requester** (who asked: you, or Admin when it delegates) and the **executor** (the actor that does the work).
Lineage has two different links: `forked_from` carries conversation history up to an edit point; `delegated_from` carries
a scoped assignment and only the context the worker needs.
