# Voice runtime: a second agent runtime beside Hermès

Draft, 9 November 2026. Branch `paw-voice-runtime`, created from `version-1` (8ce5b74).
This is a feature slice of its own. It does not change the Story 2 plan and does not wait for memory (002E),
the agent registry (PAW-004) or dispatch (PAW-005).

## Goal

A voice agent that holds real phone calls through Twilio and sits at the same level as Admin Hermès:
its own actor, its own runtime, its own credentials, every tool call through the PAW gateway. Hermès can
ask it to make a call and read the outcome. Voice Foundry (`cerebrium-voice-agent`) is the source of the
Twilio and GPT-Live code; it is ported, not merged.

## What already fits

- `modules/runtime/protocol.py` is the runtime port. PAW depends on it, not on Hermès's wire format.
  A voice adapter that implements `create_run`, `get_run`, `stop_run`, `stream_events` and `healthy` needs no
  change in the worker, ledger, recovery or deadlines.
- The runtime registry (migration 0015) already binds runtime → actor → hashed gateway and model credentials,
  one executing run per actor, and per-runtime queue claims.
- The gateway already resolves a credential to a runtime on every request, so policy, audit and budgets apply
  to voice tool calls the same way they do to Hermès.
- Approvals (migration 0017) can gate an outbound call.

## What has to change

1. `agent_runtimes.kind` (`hermes` | `voice`, default `hermes`), migration 0018. `registry.adapter()` becomes a
   small factory on `kind`. Today it always returns `HermesAdapter`. This is the only core change.
2. A `VoiceAdapter` (port implementation) and a voice service process that owns the Twilio webhooks, the media
   stream and the GPT-Live session.
3. A `voice-agent` actor with its own profile (persona, allowed tools, budgets) and a registered runtime.
4. A gateway tool `voice_call_request(contact, agenda)` that Hermès can use. It creates a run for `voice-agent`
   and requires owner approval before dialling. It stands in for PAW-005 dispatch until that exists.

## Port from Voice Foundry (keep) and leave behind (replaced by PAW)

| Keep, adapted | Why |
|---|---|
| `live_voice.py` (Twilio µ-law ↔ GPT-Live bridge, barge-in) | the part that is unique to voice |
| `voice_behavior.py`, call plan / agenda handling | call behaviour, not platform |
| Consent and do-not-call checks from contacts | must run before any dial |

| Replace with PAW | Voice Foundry module |
|---|---|
| Tool calls | `v3/live_functions.py`, `tool_gateway.py` → PAW gateway client |
| Memory | `v3/memory.py`, `intelligent_memory.py` → ledger now, PAW memory (002E) later |
| Auth, registry, integrations | `security.py`, `v3/registry.py`, `integrations/*` → PAW identity and connectors |

## Known gaps, stated up front

- **Public HTTPS.** Twilio needs a reachable HTTPS and WSS URL. PAW remote access (A4) is not built. In
  development use a tunnel. This also gates any real phone test.
- **Realtime cost control.** The A1 model proxy meters text requests. A GPT-Live socket is not covered. This
  slice enforces a per-call maximum duration and a per-call spend cap in the voice service, and records usage
  against the actor. Extending the proxy to realtime is a later decision.
- **Memory.** Until 002E, call outcomes go to the ledger only. The agent cannot recall earlier calls.
- **Accuracy of tests.** Twilio and GPT-Live are faked in automated tests. A real call is a manual owner check.

## Slices and acceptance

1. **Runtime kind and voice actor.** Migration 0018, adapter factory, `voice` runtime registration command,
   voice-agent profile. Accept: existing Admin tests unchanged and green; a registered `voice` runtime is
   claimed by its own worker and never touches Admin's runs; credentials stay separated.
2. **Voice service with fake telephony.** `VoiceAdapter` plus service, run lifecycle mapped to the port,
   duration and spend caps, transcript and outcome stored as run events. Accept: fake Twilio/GPT-Live
   end-to-end run, cancel, timeout and crash recovery.
3. **Twilio and GPT-Live bridge.** Ported audio bridge calling gateway tools with the voice runtime's own
   credential. Accept: fake-socket tests for barge-in and tool calls; denied tools are denied by the gateway.
4. **Hermès tool behind approval.** `voice_call_request` with consent check, approval, and result readback.
   Accept: no dial without approval; replay is idempotent; revoked consent blocks the call.
5. **Owner check (manual).** One real call through a tunnel to the owner's own number.

Slices 1 and 2 are independent of Twilio and can be fully tested offline.

## Status

- **Slice 1 done (9 November 2026):** migration `0018` (`agent_runtimes.kind`, immutable; inert `voice-agent` actor),
  `registry.register(kind=...)` with kind/profile-harness matching, adapter factory keyed on kind (voice has no adapter
  yet and refuses clearly), `DEFAULT_VOICE_SPEC`, `runtime_registry register --kind voice`, 12 new tests in
  `tests/runtime/test_runtime_kind.py`, and one expected update to `test_seeded_actors`. Operating notes are in
  [runtime registry operations](../paw-002/operations-runtime-registry.md).
- **Found while building slice 1:** with `PAW_GATEWAY_ATTESTATION=required` the gateway and worker expect Hermès-style
  runtime reports. The voice service must file them too (slice 3) or run with attestation off meanwhile.
- **Decision for the owner:** the default voice profile grants only `calendar_list_events`. Giving the voice agent
  document access is a deliberate later grant.
