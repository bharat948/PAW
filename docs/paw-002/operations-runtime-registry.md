# Operating the runtime registry

This slice registers runtimes and routes work. It does not create actors, approve
contracts, launch containers or supervise them. Those are separate foundation
slices. GitHub integration follows this slice.

## Existing Admin installation

Drain active agent runs before upgrading. Apply migration `0015` using the normal
local migration command, then restart the gateway, API and agent worker. Do not
apply this migration to a shared environment without the owner's authorization.

The gateway imports `PAW_GATEWAY_TOKEN` and `PAW_MODEL_PROXY_TOKEN` as hashes bound
to the configured Admin runtime (`PAW_GATEWAY_RUNTIME`, normally `hermes`). The
Admin worker imports `PAW_HERMES_URL` and the secret reference `PAW_HERMES_KEY`.
If using a custom Admin runtime name, set `PAW_AGENT_RUNTIME_NAME` to the same
value as `PAW_GATEWAY_RUNTIME` for its worker.
Existing runtime, session, message and run IDs are preserved. Secrets remain in
the environment; the registry stores the outbound variable name, not its value.
`./dev.sh` continues to supply the existing Admin configuration.

Restarting never restores revoked credentials, nor changes a stopped runtime's
desired state. If bootstrap sees a different token while a live credential still
exists, it refuses: explicitly rotate first. Keep the runtime's secret files and
PAW process environment consistent when rotating.

## Registering another runtime

The agent actor and its active compiled profile must already exist. This slice
does not provide actor creation. Configure and start that actor's Hermès container
manually with its profile, unique API key, gateway token and model token. Never
give it the upstream model-provider key.

From `paw-server`, with the same PAW database configuration as the other services:

```sh
uv run python -m paw_server.runtime_registry register \
  --name research --actor research-hermes \
  --url http://127.0.0.1:8643 --key-env PAW_RESEARCH_KEY

uv run python -m paw_server.runtime_registry rotate \
  --name research --purpose gateway --token-env PAW_RESEARCH_GATEWAY_TOKEN
uv run python -m paw_server.runtime_registry rotate \
  --name research --purpose model --token-env PAW_RESEARCH_MODEL_TOKEN

PAW_AGENT_RUNTIME_NAME=research uv run python -m paw_server.agent_worker
```

Supply those environment variables securely before running the commands; token
values never appear as arguments or in output. The worker and API processes that
must contact the container need the referenced outbound API-key variable exported
in their environments. Use distinct, randomly generated tokens of at least 24
ASCII characters. Each credential belongs to one runtime and one purpose.

Run one worker process per runtime. Several runtimes can process different actors
concurrently; duplicate workers cannot claim a second executing run for the same
actor. Queued work waits while its actor is busy or its runtime is stopped.
This slice starts no containers and does not implement the later three-container
supervisor capacity policy.

Owner-authenticated `POST /v1/agent-sessions` accepts `agent_actor_id` (default
`admin-hermes`) to select an existing registered agent. Run admission, history,
forking and privacy controls use that session's actor. There is no actor selector
UI in this slice. Secondary actors do not require legacy Admin runtime settings
to admit or cancel work.

## Observe, rotate, revoke and stop

```sh
uv run python -m paw_server.runtime_registry list
uv run python -m paw_server.runtime_registry rotate \
  --name research --purpose gateway --token-env PAW_RESEARCH_NEW_GATEWAY_TOKEN
uv run python -m paw_server.runtime_registry revoke --name research --purpose model
uv run python -m paw_server.runtime_registry stop --name research
uv run python -m paw_server.runtime_registry start --name research
```

Listing shows actor, desired state/profile, observed boot status, observation time
and credential purposes; it exposes no tokens, hashes or secret values. A boot
report updates observed status against the desired compiled config. Desired profile
versions follow owner activation; in-flight runs retain their original profile.
With required attestation, new claims wait until the runtime reports a matching
boot config. As before, model/tool changes require manually recreating the
container with the compiled configuration.

Rotation revokes the previous token. Revocation applies to subsequent HTTP requests
on existing MCP sessions as well as new connections. Tool and model admission also
recheck captured credentials. MCP sessions are credential-bound, including their
SSE and delete routes; reconnect after rotation. Already admitted upstream requests may still finish
and settle their costs. Revoking a token cannot undo an external request already
sent.

`stop` sets desired state to stopped and revokes both inbound credentials. It does
not stop the container: cancel active work through the owner API, then stop the
manually managed container. `start` changes desired state only; provision fresh
tokens after stopping. Revoked tokens cannot be reactivated. Drain before changing
the registered endpoint or its secret reference; registration refuses while a
runtime holds a run lease. Runtime-to-actor bindings cannot be changed.

## Verification

`tests/runtime/test_registry.py` exercises two actors, concurrent dispatch, database
constraints, purpose separation, rotation and persistent revocation, real MCP HTTP,
model proxy isolation with a fake provider, report isolation, boot/profile checks,
secondary recovery/deadlines/cancellation/privacy, endpoint resolution, and upgrade
from the previous schema. It also verifies report cleanup with colliding local
Hermès run IDs. All databases are disposable; these tests make no paid calls.

The slice uses the existing Admin, gateway, cost and ledger regression suites as
compatibility gates. Live model, Google Calendar, HTTPS and outside-network phone
acceptance remain outstanding tasks, alongside the later GitHub connector.
