#!/usr/bin/env bash
# One command for local dev: infra in Docker; API, worker and web run on the host with hot reload.
#   ./dev.sh          start everything (Ctrl-C stops the three app processes; infra keeps running)
#   ./dev.sh stop     also stop the Docker infra and Hermès
#   HERMES=0 ./dev.sh start without the Hermès container
set -euo pipefail
cd "$(dirname "$0")"

# Admin Hermès (PAW-002) runs in its own container and can only act through the PAW gateway (MCP, on the host).
# Started when the image (docker build, see deploy/hermes/README.md) and the secrets file exist. HERMES=0 skips it.
HERMES_ENV="${PAW_HERMES_ENV:-$HOME/.config/paw/s0.env}"          # secrets: OpenAI key, Hermès API key (outside the repo)
HERMES_GEN="$HOME/.config/paw/hermes.generated.env"               # compiled from the active profile at every start
GATEWAY_TOKEN_FILE="$HOME/.config/paw/gateway.token"
PROXY_TOKEN_FILE="$HOME/.config/paw/model-proxy.token"                # the runtime's credential for the PAW model proxy
HERMES_COMPOSE=(docker compose --env-file "$HERMES_ENV" -f deploy/hermes/compose.s0.yaml)
[[ -f "$HERMES_GEN" ]] && HERMES_COMPOSE=(docker compose --env-file "$HERMES_ENV" --env-file "$HERMES_GEN" -f deploy/hermes/compose.s0.yaml)

if [[ "${1:-}" == "stop" ]]; then
  [[ -f "$HERMES_ENV" ]] && "${HERMES_COMPOSE[@]}" down 2>/dev/null || true
  make -C paw-server dev-infra-down; exit 0
fi

[[ -f paw-server/.env ]] || { cp paw-server/.env.example paw-server/.env; echo "created paw-server/.env"; }
[[ -f paw-web/.env ]] || { cp paw-web/.env.example paw-web/.env; echo "created paw-web/.env"; }
[[ -d paw-web/node_modules ]] || (cd paw-web && npm ci)
(cd paw-server && uv sync --frozen)

make -C paw-server dev-infra
make -C paw-server migrate

pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true' EXIT INT TERM
prefix() { sed -u "s/^/[$1] /"; }

start_hermes() {
  [[ "${HERMES:-1}" == "0" ]] && { echo "hermes: skipped (HERMES=0)"; return; }
  if ! docker image inspect paw-hermes:s0 >/dev/null 2>&1; then echo "hermes: image paw-hermes:s0 not built (deploy/hermes/README.md step 1), skipping"; return; fi
  if [[ ! -f "$HERMES_ENV" ]]; then echo "hermes: $HERMES_ENV missing (deploy/hermes/README.md step 2), skipping"; return; fi
  if [[ ! -f "$GATEWAY_TOKEN_FILE" ]]; then (umask 077; openssl rand -hex 24 > "$GATEWAY_TOKEN_FILE"); fi
  if [[ ! -f "$PROXY_TOKEN_FILE" ]]; then (umask 077; openssl rand -hex 24 > "$PROXY_TOKEN_FILE"); fi
  # The provider key stays on the host (the PAW gateway process); the Hermès container only gets the proxy token.
  export PAW_MODEL_PROXY_TOKEN="$(cat "$PROXY_TOKEN_FILE")" PAW_MODEL_PROXY_UPSTREAM_KEY="$(grep '^OPENAI_API_KEY=' "$HERMES_ENV" | cut -d= -f2-)"
  export PAW_MODEL_PROXY_URL="http://host.docker.internal:8100/v1"
  export PAW_GATEWAY_TOKEN="$(cat "$GATEWAY_TOKEN_FILE")" PAW_GATEWAY_ATTESTATION=required PAW_GATEWAY_HOST="${PAW_GATEWAY_HOST:-0.0.0.0}"
  # 1. the gateway first: Hermès parks a tool server that is down at its start and never retries it
  (cd paw-server && make gateway 2>&1 | prefix gateway) & pids+=($!)
  for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8100/_health && break; sleep 1; done
  # 2. the container's settings, compiled from the active profile (tool list, model, turn limit) and the gateway address
  ( umask 077
    { (cd paw-server && uv run python -m paw_server.profile_export)
      echo "PAW_MCP_URL=http://host.docker.internal:8100/mcp"; echo "PAW_MCP_TOKEN=$PAW_GATEWAY_TOKEN"
      echo "PAW_MODEL_PROXY_TOKEN=$PAW_MODEL_PROXY_TOKEN"; } > "$HERMES_GEN" )
  HERMES_COMPOSE=(docker compose --env-file "$HERMES_ENV" --env-file "$HERMES_GEN" -f deploy/hermes/compose.s0.yaml)
  # 3. recreate so a profile change is applied (this is the "restart" the Profile page asks for)
  "${HERMES_COMPOSE[@]}" up -d --force-recreate hermes >/dev/null
  for _ in $(seq 1 60); do [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8642/v1/capabilities)" == "401" ]] && { export PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY="$(grep '^API_SERVER_KEY=' "$HERMES_ENV" | cut -d= -f2-)"; echo "hermes http://127.0.0.1:8642 (up), gateway http://127.0.0.1:8100"; return; }; sleep 1; done
  echo "hermes: did not become ready, see: docker logs paw-hermes-s0-hermes-1"
}
start_hermes

(cd paw-server && make run 2>&1 | prefix api) & pids+=($!)
(cd paw-server && make worker 2>&1 | prefix worker) & pids+=($!)
(cd paw-server && make session-worker 2>&1 | prefix sessions) & pids+=($!)
if [[ -n "${PAW_BACKUP_AGE_RECIPIENT:-}" && -n "${PAW_HOME_ENV:-}" ]]; then
  (uv run --project paw-server --with pyrage python scripts/backup/pawbackup.py sync-deletions --watch 2>&1 | prefix privacy-sync) & pids+=($!)
fi
if [[ -n "${PAW_HERMES_URL:-}" ]]; then (cd paw-server && make agent-worker 2>&1 | prefix agent) & pids+=($!); fi
(cd paw-web && npm run dev 2>&1 | prefix web) & pids+=($!)
echo "web http://localhost:5173  |  api http://localhost:8000/docs  |  token in paw-server/.env (PAW_OWNER_TOKEN)"
wait
