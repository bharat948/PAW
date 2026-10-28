# PAW

Personal agent workspace. Current milestone: **PAW 001 – Cloud RAG Foundation**.

- `paw-server/`: FastAPI API, Postgres + pgvector, ingestion worker
- `paw-web/`: React + Vite + TypeScript web app

Monorepo for now (CI per directory); see each README. Local runtime: `cd paw-server && make up`.

## Local development (hot reload, no app containers)

```
./dev.sh          # infra in Docker (Postgres + S3), then API :8000, worker, and web :5173 with hot reload
./dev.sh stop     # also stop the Docker infra
```

Edit `paw-server/src` or `paw-web/src` and both sides reload. Settings come from `paw-server/.env` and
`paw-web/.env` (created from the `.env.example` files on first run; add your embedding key there).
Sign in at http://localhost:5173 with `PAW_OWNER_TOKEN`. Individual pieces: `make -C paw-server run|worker|migrate|dev-infra`
and `npm run dev` in `paw-web`.

Notes: running the dev setup stops the `api`/`worker`/`web` containers (same ports). SeaweedFS can take ~10 s on its
first write after starting, so the very first upload may be slow. `make up` still runs the whole stack in Docker.
