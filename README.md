# PAW

Personal agent workspace. Current milestone: **PAW 001 – Cloud RAG Foundation**.

- `paw-server/`: FastAPI API, Postgres + pgvector, ingestion worker
- `paw-web/`: React + Vite + TypeScript web app

Monorepo for now (CI per directory); see each README. Local runtime: `cd paw-server && make up`.
