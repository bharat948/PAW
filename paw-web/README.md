# paw-web

React + Vite + TypeScript browser app for PAW (spec: PAW-001, substory 001A).
Sign in with the owner token, upload documents (progress + durable status), search with source references, and a status page.

## Run

Full stack (db, api, worker, web) is started from `paw-server`: see its README (`make up`, then http://localhost:8080).

Standalone dev server:

```
nvm use            # Node from .nvmrc
npm ci
cp .env.example .env   # set VITE_API_BASE_URL
npm run dev
```

## Checks (identical to CI)

```
npm run check      # lint + typecheck + unit tests
npm run build
npx playwright install chromium && npm run test:e2e   # mocked API, desktop + mobile
npm run test:live  # real browser vs the running stack (`make up`): upload -> status -> search -> source
```

## Notes

- API base URL is build-time `VITE_API_BASE_URL`; it is inlined in the bundle, so it must never hold secrets.
- Errors from the API use one envelope (`code, message, correlation_id, retryable`); the UI shows only
  user-safe text plus the correlation ID as a support reference.
- The browser never talks to Postgres, object storage, or embedding providers.
