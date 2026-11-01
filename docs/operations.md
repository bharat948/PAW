# PAW operations runbook: start, restart, debug

Scope: PAW-001D (`PAW-001D_Cloud_Deployment_Foundation.md`, section 17 runbooks).
Each environment is marked **works today** or **planned**. Nothing planned is described as done.

| Environment | Status | Entry point |
|---|---|---|
| Development (host processes + Docker infra) | works today | `./dev.sh` |
| Full stack in Docker | works today | `make -C paw-server up` |
| Home stack (compose + proxy, manual commands) | works today, tested once | `docs/environment-contract.md` |
| Home launcher (`pawctl`, Start PAW, tunnel, backups) | **planned**, not implemented | see 001D sections 7, 10 |
| VPS / cloud | **planned** | 001D section 23 |

## 1. Ports and services

| What | Dev (`./dev.sh`) | Docker (`make up`) |
|---|---|---|
| Web | http://localhost:5173 | http://localhost:8080 |
| API | http://localhost:8000 (`/docs`) | http://localhost:8000 |
| Postgres + pgvector | localhost:5432 | internal only |
| S3 (SeaweedFS) | localhost:9000 | internal only |
| Worker | host process | container |

Dev and Docker use the same API/web ports for api/worker, so they cannot run together. `dev.sh` stops the app containers first.
Sign in with `PAW_OWNER_TOKEN` from `paw-server/.env`.

## 2. Development

```
./dev.sh          # start (infra in Docker; api, worker, web on host with hot reload)
./dev.sh stop     # also stop Docker infra (db, s3). Data volumes are kept.
```

Ctrl-C stops api/worker/web only; Postgres and S3 keep running.

**Restart after changing `paw-server/.env`:** the API reloads on file changes under `src`, but `.env` is read at startup. Do a full restart:
1. Ctrl-C the `./dev.sh` terminal.
2. Check nothing is left: `ps aux | grep -E "uvicorn|watchfiles|vite"`. Kill leftovers by PID (`kill <pid>`).
3. `./dev.sh`

If you started `dev.sh` in the background, kill by PID. Do not use `pkill -f` with a pattern that also matches your own shell command line.

**Restart one piece:** `make -C paw-server run`, `make -C paw-server worker`, `npm run dev` (in `paw-web`).

**Prerequisite:** the Docker daemon must be running. On Linux Docker Desktop: `systemctl --user start docker-desktop`.

## 3. Full stack in Docker

```
cd paw-server
make up      # build and start db, migrate, api, worker, web
make down    # stop; data volumes kept
docker compose down -v   # DESTRUCTIVE: deletes database and object data
```

Restart one service: `docker compose restart api` (or `worker`). After a code change: `docker compose up --build -d api worker web`.

## 4. Health checks (run these first)

```
curl localhost:8000/health   # {"status":"alive"}  process is up only
curl localhost:8000/ready    # {"status":"ready"}  DB reachable and migrations at head; 503 otherwise
```

`/health` OK with `/ready` failing means the DB is down or migrations are behind.

## 5. Debugging playbook

Start with the logs: they are JSON, one event per line, and carry a `correlation_id`.
Dev: output is prefixed `[api]`, `[worker]`, `[web]`. Docker: `docker compose logs -f api worker`.
The API error envelope is `{code, message, correlation_id, retryable, details?}`. Grep logs for that `correlation_id` to follow one request.

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `dev.sh`: "Cannot connect to the Docker daemon" | Docker not running | Start Docker (section 2), rerun |
| Port 8000/5173 already in use | Leftover process or app containers | `ss -ltnp \| grep -E ':8000\|:5173'`; kill the PID or `docker compose stop api worker web` |
| Web shows 401 / login loops | Wrong or missing `PAW_OWNER_TOKEN` (token mode) or an expired Google session | Compare token in `paw-server/.env`; sign out and in |
| Google sign-in problems (redirect_uri_mismatch, not allowed, no button) | OAuth client, allow-list or an API not restarted after `.env` edits | `docs/auth-google-setup.md`, section 4 |
| `/ready` 503 | Postgres down or migrations behind | `docker ps`; `make -C paw-server migrate` |
| Upload stays "pending" | Worker not running or erroring | Look for `[worker]` lines; `paw.worker` "worker loop error" |
| Worker logs `AdminShutdown` / "failed to resolve host 'db'" | Postgres restarted or stopped under it | Normal during a DB restart; the worker reconnects. If it persists, restart the worker |
| Chat/Ask: "language model is busy or unavailable" | OpenAI returned 429 or 5xx | Logs show `HTTP Request: POST https://api.openai.com/... 429`. 429 can mean rate limit **or** no credits: see below |
| Chat/Ask: "rejected the API credentials" | Bad `PAW_EMBEDDING_API_KEY` / `PAW_LLM_API_KEY` | Fix key in `.env`, full restart |
| Search results only keyword (trace shows `embedding_unavailable`) | Embedding call failed (key, quota, network) | Same OpenAI check; search still works in degraded mode |
| First upload very slow | SeaweedFS needs ~10 s after start | Wait and retry |
| Slow or odd behavior after a DB restart | Stale pool connections | Restart api and worker |

**Tell a rate limit from no credits** (does not print the key):
```
cd paw-server && set -a && . ./.env && set +a
curl -s https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $PAW_EMBEDDING_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```
`insufficient_quota` / `credit_balance_exhausted` means add credits; `rate_limit_exceeded` means wait.

**Run without a provider key:** set `PAW_LLM_PROVIDER` to anything other than `openai` to use the offline extractive model. Embeddings still call OpenAI unless the embedding provider is changed too.

**Database shell:** `docker compose -f paw-server/compose.yaml -f paw-server/compose.dev.yaml exec db psql -U paw paw` (dev password is in `compose.yaml`).

**Tests:** `make -C paw-server check` (lint, types, unit), `make -C paw-server test-integration` (needs Postgres), `npm run check` and `npm run test:e2e` in `paw-web`.

## 6. Home deployment (planned)

Partly built (001D-01): `deploy/compose.home.yaml`, proxy and config checks exist; see `docs/environment-contract.md` for the manual commands. Not built yet: `pawctl`, Start PAW launcher, tunnel, backups, CI releases. When built, this section should document, per the spec:
- Start / status / stop through one controller (001D 7.5, 10) and the status states (10.3).
- Boot policy versus maintenance hold (10.5).
- Update, rollback, backup and restore (14.4, 14.5, 16).
- Operator-facing troubleshooting messages (17.5).

Until then the home stack is started by hand with the compose commands in the environment contract, with no remote access or off-device backup.

## 7. Safe vs destructive

Safe: `./dev.sh`, `./dev.sh stop`, `make up`, `make down`, `docker compose restart <svc>`, `make migrate`.
Destructive: `docker compose down -v`, deleting Docker volumes, editing the DB by hand. These delete or change stored documents; there is no backup yet.
