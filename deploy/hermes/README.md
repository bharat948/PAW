# Running the Hermès spike stack (PAW-002 S0)

**What this is:** the real Hermès runtime plus a stub MCP tool server, used to prove the integration surface (see
`docs/paw-002/s0-findings.md`). **What this is not:** PAW chat. Nothing in `paw-server` or `paw-web` talks to Hermès yet;
the PAW-to-Hermès bridge is phase 002A. So today there are two independent stacks:

| Stack | Start | Talks to |
|---|---|---|
| PAW (API, worker, web, Postgres, S3) | `./dev.sh` | OpenAI directly (Search, Ask) |
| Hermès spike (Hermès + stub MCP) | steps below | OpenAI (model) and the stub MCP server |

## Prerequisites
Docker running (`systemctl --user start docker-desktop` on this laptop), about 4 GB disk for the image, an OpenAI key with credit.

## 1. Build the image (once; about 10 minutes on a slow link, retry on a transient GitHub error)
```
docker build -t paw-hermes:s0 \
  --build-arg INSTALL_SH_SHA256=abf338605dae6dd28d101673ac6aed7995c955745db46d47676bc591a7957209 \
  --build-arg HERMES_COMMIT=c7da41dd522dc19cc08bbfcfa69d4b078224b45d deploy/hermes
```
The installer is verified by hash and Hermès is pinned to an exact commit. Rebuild after editing `entrypoint.sh` or `plugins/`
(only the last layers rebuild, so it takes seconds).

## 2. Create the env file OUTSIDE the repo
```
mkdir -p ~/.config/paw && umask 077
{
  echo "API_SERVER_KEY=s0$(openssl rand -hex 24)"
  echo "STUB_TOKEN=stub$(openssl rand -hex 20)"
  echo "OPENAI_API_KEY=$(grep -E '^PAW_(LLM|EMBEDDING)_API_KEY=.+' paw-server/.env | head -1 | cut -d= -f2-)"
  echo "HERMES_CONFIG_SET=model.provider=openai-api;model.default=gpt-4o-mini"
  echo "PAW_MCP_URL=http://stub-mcp:8001/mcp"
  echo "PAW_MCP_INCLUDE=paw_echo, paw_write_note, paw_slow"
} > ~/.config/paw/s0.env
```
Never commit this file or paste it anywhere: it holds your OpenAI key. Use a dedicated OpenAI project key with a monthly budget.

## 3. Start, check, stop
**Shortcut:** once steps 1 and 2 are done, `./dev.sh` starts the PAW gateway, recreates the Hermès container from the active profile (tool list, model, turn limit) and starts the agent worker with the rest of PAW. `./dev.sh stop` stops them; `HERMES=0 ./dev.sh` skips them. Re-running `./dev.sh` is the "restart Hermès" the Profile page asks for. The commands below are the S0 spike with the stub tool server.

```
docker compose --env-file ~/.config/paw/s0.env -f deploy/hermes/compose.s0.yaml up -d
curl -s -o /dev/null -w "%{http_code}\n" localhost:8642/v1/capabilities     # 401 = up and rejecting unauthenticated calls
docker logs paw-hermes-s0-hermes-1 2>&1 | grep -i -E "Disabled|paw-mcp|MCP server"   # lockdown applied, MCP connected
docker compose --env-file ~/.config/paw/s0.env -f deploy/hermes/compose.s0.yaml down   # stop (nothing persistent to lose)
```
**Start order matters:** the stub MCP server must be healthy before Hermès starts. If it was down at start, Hermès parks it
**permanently** (findings section 3) and the model may *claim* tool use that never happened. Fix: restart the Hermès container.
After editing the env file, recreate (`up -d --force-recreate hermes`); a plain restart keeps the old values.

## 4. Talk to it
```
deploy/hermes/s0-chat.sh ~/.config/paw/s0.env "Call paw_echo with text hello and tell me what came back."
deploy/hermes/s0-chat.sh ~/.config/paw/s0.env "What is 17 times 23?"
curl -s localhost:8001/_calls          # what the stub tool server actually executed (the evidence, not the model's words)
```
It answers with `gpt-4o-mini` and can only call the three stub tools. It has no PAW identity, no memory, no documents, no approvals.

## 5. Run the contract tests
```
cd paw-server
PAW_STUB_URL=http://127.0.0.1:8001 PAW_HERMES_URL=http://127.0.0.1:8642 \
PAW_HERMES_KEY=$(grep ^API_SERVER_KEY ~/.config/paw/s0.env | cut -d= -f2) \
  uv run pytest tests/contract/hermes          # expect: 19 passed, 1 xfailed (costs a few cents)
uv run pytest tests/runtime tests/contract     # with the same env vars: also runs the adapter against the real container
uv run pytest tests/contract                   # no Hermès needed: runs against the fake
```

## Image defaults (secure) and experiment knobs
On: native-tool lockdown, Hermès memory off, guard plugin, MCP `resources/prompts/sampling/elicitation` off, `trust: full`.
Experiments only (env file): `PAW_HERMES_LOCKDOWN=0`, `PAW_HERMES_GUARD=0`, `PAW_GUARD_MODE=off|raise`, `PAW_MCP_TRUST=untrusted`.
Unlocked Hermès can run a shell inside its container and sends about 10k tokens of tool schemas per call.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `Cannot connect to the Docker daemon` | start Docker Desktop |
| Model says it called a tool but `/_calls` is empty | MCP server was down when Hermès started: restart Hermès |
| Run stuck `waiting_for_approval` | `PAW_MCP_TRUST=untrusted` is set; stop the run (`POST /v1/runs/{id}/stop`) or remove it |
| `401` on every call | wrong `API_SERVER_KEY`; the container reads it only at start |
| Port 8642/8001 in use | another copy is running: `docker compose ... down` |
| Answers mention it cannot see tools | the model sometimes refuses to list tools; check `/_calls` instead |

## 6. Gateway end-to-end (002D)
In dev, Hermès reaches the PAW gateway on the host (`host.docker.internal:8100`) with a bearer token kept in
`~/.config/paw/gateway.token`. The gateway listens on `0.0.0.0` in dev so the container can reach it; the token is the access
control, so keep it private (home uses a private Docker network with no published port). Start order matters: the gateway
must be up before Hermès, or Hermès parks it for good.

Manual check against the real stack (costs a few cents), with the gateway, the container and a scratch database wired together:
```
createdb paw_e2e && PAW_DATABASE_URL=.../paw_e2e uv run alembic -c alembic.ini upgrade head
PAW_DATABASE_URL=.../paw_e2e PAW_GATEWAY_TOKEN=$(cat ~/.config/paw/gateway.token) PAW_GATEWAY_ATTESTATION=required \
  PAW_GATEWAY_HOST=0.0.0.0 uv run python -m paw_server.gateway &
# container env from the default profile: see tests/identity and paw_server.profile_export, then recreate hermes
PAW_E2E_DATABASE_URL=.../paw_e2e PAW_E2E_GATEWAY_TOKEN=$(cat ~/.config/paw/gateway.token) \
PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY=... uv run pytest tests/test_gateway_real.py
```
Verified 2026-11-04 against `paw-hermes:s0`: a real run calls `paw_system_status` through the gateway and every step is stored;
Calendar not connected is denied `NOT_CONNECTED` and Hermès says so; with native toolsets switched on (`PAW_HERMES_LOCKDOWN=0`)
the model call carries 19 tools instead of 6, and 25 attempts to use `terminal` were all blocked and audited
(`NATIVE_TOOL_BLOCKED`) until `agent.max_turns` (from the profile) ended the run as FAILED.
