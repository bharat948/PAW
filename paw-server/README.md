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

## Ingestion pipeline (PAW-001B)

Upload → durable original → background worker → searchable, provenance-tagged chunks.

```
export T=paw_local_only_owner_token          # PAW_OWNER_TOKEN; single-owner bearer auth
curl -H "Authorization: Bearer $T" -F file=@report.pdf localhost:8000/v1/documents     # 202
curl -H "Authorization: Bearer $T" localhost:8000/v1/documents                          # list (?status=, cursor)
curl -H "Authorization: Bearer $T" localhost:8000/v1/documents/<id>                     # versions, stage, events
curl -H "Authorization: Bearer $T" localhost:8000/v1/documents/<id>/chunks              # passages + provenance
```

| Kind | Formats | Chunk provenance |
|---|---|---|
| Unstructured | `.txt` `.md` `.pdf` (text) | char offsets; PDF page number; Markdown heading path |
| Structured | `.csv` `.tsv` `.json` `.jsonl` | row ranges; column names; raw records kept in `metadata` |

- **States:** RECEIVED → STORED → EXTRACTING → CHUNKING → EMBEDDING → INDEXING → COMPLETED, or FAILED / UNSUPPORTED
  (image-only PDFs, empty files). Every transition is an `ingestion_events` row.
- **Durability:** original stored content-addressed (sha256) in S3-compatible storage *before* the job is queued;
  integrity re-verified by the worker. Jobs live in Postgres (`FOR UPDATE SKIP LOCKED`, leases, fenced writes).
- **Recovery:** chunks are written once (checkpoint), embeddings are committed per batch; a killed worker's job is
  re-claimed after the lease expires and only embeds the remainder. Retries use bounded exponential backoff with
  jitter; exhausted jobs are dead-lettered (`dead_letter=true`, FAILED).
- **Idempotency:** same bytes + same processing config (parser version, chunk size/overlap, embedding model) = same
  version, even under concurrent uploads. Changing config yields version N+1 of the same document; only the
  current version's chunks are "active". Optional `Idempotency-Key` header.
- **Embeddings:** adapter interface; `PAW_EMBEDDING_PROVIDER=fake` (default, deterministic, free) or `openai`
  (any OpenAI-compatible endpoint). Schema is `vector(1536)`; changing dimensions requires a reindex.
  Daily per-workspace token budget: `PAW_EMBEDDING_DAILY_TOKEN_BUDGET`.
- **Scale out:** run more worker replicas (`docker compose up -d --scale worker=3`).
- **Local object store:** SeaweedFS in Compose (`s3` service); production uses any S3-compatible bucket via `PAW_S3_*`.

Not yet (later substories): hybrid search endpoint (001C), rate limiting, retention/deletion, restricted DB roles (001D).
