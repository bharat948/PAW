#!/usr/bin/env bash
# One command for local dev: infra in Docker; API, worker and web run on the host with hot reload.
#   ./dev.sh          start everything (Ctrl-C stops the three app processes; infra keeps running)
#   ./dev.sh stop     also stop the Docker infra and Hermès
#   HERMES=0 ./dev.sh start without the Hermès container
set -euo pipefail
cd "$(dirname "$0")"

# Hermès (Admin agent runtime, PAW-002) runs in its own container, with a stub tool server until the PAW gateway exists.
# Started when the image (docker build, see deploy/hermes/README.md) and the env file exist. HERMES=0 skips it.
HERMES_ENV="${PAW_HERMES_ENV:-$HOME/.config/paw/s0.env}"
HERMES_COMPOSE=(docker compose --env-file "$HERMES_ENV" -f deploy/hermes/compose.s0.yaml)

start_hermes() {
  [[ "${HERMES:-1}" == "0" ]] && { echo "hermes: skipped (HERMES=0)"; return; }
  if ! docker image inspect paw-hermes:s0 >/dev/null 2>&1; then echo "hermes: image paw-hermes:s0 not built (deploy/hermes/README.md step 1), skipping"; return; fi
  if [[ ! -f "$HERMES_ENV" ]]; then echo "hermes: $HERMES_ENV missing (deploy/hermes/README.md step 2), skipping"; return; fi
  "${HERMES_COMPOSE[@]}" up -d stub-mcp >/dev/null
  # the tool server must answer before Hermès starts, or Hermès parks it for good
  for _ in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8001/_calls && break; sleep 1; done
  "${HERMES_COMPOSE[@]}" up -d hermes >/dev/null
  for _ in $(seq 1 60); do [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8642/v1/capabilities)" == "401" ]] && { export PAW_HERMES_URL=http://127.0.0.1:8642 PAW_HERMES_KEY="$(grep '^API_SERVER_KEY=' "$HERMES_ENV" | cut -d= -f2-)"; echo "hermes http://127.0.0.1:8642 (up)"; return; }; sleep 1; done
  echo "hermes: did not become ready, see: docker logs paw-hermes-s0-hermes-1"
}

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
start_hermes

pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true' EXIT INT TERM
prefix() { sed -u "s/^/[$1] /"; }
(cd paw-server && make run 2>&1 | prefix api) & pids+=($!)
(cd paw-server && make worker 2>&1 | prefix worker) & pids+=($!)
if [[ -n "${PAW_HERMES_URL:-}" ]]; then (cd paw-server && make agent-worker 2>&1 | prefix agent) & pids+=($!); fi
(cd paw-web && npm run dev 2>&1 | prefix web) & pids+=($!)
echo "web http://localhost:5173  |  api http://localhost:8000/docs  |  token in paw-server/.env (PAW_OWNER_TOKEN)"
wait
