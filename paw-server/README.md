# paw-server

FastAPI API and ingestion worker for PAW (spec: PAW-001, substory 001A).

## Quick start (local)

Requires Docker, `uv`, `make`. `paw-web` is the sibling directory in this monorepo.

```
git clone https://github.com/bharat948/PAW.git
cd PAW/paw-server
make up            # builds and starts db, migrate, api, worker, web
curl localhost:8000/health   # liveness
curl localhost:8000/ready    # readiness (503 if DB down / migrations behind)
open http://localhost:8080   # web shell shows backend + database readiness
make down          # stop (add `-v` via `docker compose down -v` to drop data)
```

No manual SQL or hidden env files: migrations run via the one-shot `migrate` service.

## Development

```
make install       # uv sync --frozen (lockfile-exact)
make check         # lint + typecheck + unit tests (same commands CI runs)
make run           # API with reload; needs PAW_DATABASE_URL (see .env.example)
```

Integration tests (real pgvector Postgres, throwaway database per test):

```
docker compose -f compose.yaml -f compose.dev-db.yaml up -d db
make test-integration
```

## Conventions

- Layout: `src/paw_server/{api,core,db,modules}`; migrations in `db/migrations`.
- Request handlers never create tables or migrate; only `alembic upgrade head` does.
- `/health` = process liveness only. `/ready` = DB reachable and `alembic_version` equals head.
- Every error uses one envelope: `{code, message, correlation_id, retryable, details?}`.
- Config is `PAW_*` env vars, validated at startup (`core/config.py`); `local|ci|staging|production`.
  Credentials are injected at runtime and never committed.
