#!/usr/bin/env bash
# One command for local dev: infra in Docker; API, worker and web run on the host with hot reload.
#   ./dev.sh          start everything (Ctrl-C stops the three app processes; infra keeps running)
#   ./dev.sh stop     also stop the Docker infra
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "stop" ]]; then make -C paw-server dev-infra-down; exit 0; fi

[[ -f paw-server/.env ]] || { cp paw-server/.env.example paw-server/.env; echo "created paw-server/.env"; }
[[ -f paw-web/.env ]] || { cp paw-web/.env.example paw-web/.env; echo "created paw-web/.env"; }
[[ -d paw-web/node_modules ]] || (cd paw-web && npm ci)
(cd paw-server && uv sync --frozen)

make -C paw-server dev-infra
make -C paw-server migrate

pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true' EXIT INT TERM
prefix() { sed -u "s/^/[$1] /"; }
(cd paw-server && make run 2>&1 | prefix api) & pids+=($!)
(cd paw-server && make worker 2>&1 | prefix worker) & pids+=($!)
(cd paw-web && npm run dev 2>&1 | prefix web) & pids+=($!)
echo "web http://localhost:5173  |  api http://localhost:8000/docs  |  token in paw-server/.env (PAW_OWNER_TOKEN)"
wait
