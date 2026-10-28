# paw-web

React + Vite + TypeScript browser app for PAW (spec: PAW-001, substory 001A).
Currently a responsive, accessible shell that calls the API's `/ready` and shows backend/database status.

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
npx playwright install chromium && npm run test:e2e   # desktop + mobile viewports
```

## Notes

- API base URL is build-time `VITE_API_BASE_URL`; it is inlined in the bundle, so it must never hold secrets.
- Errors from the API use one envelope (`code, message, correlation_id, retryable`); the UI shows only
  user-safe text plus the correlation ID as a support reference.
- The browser never talks to Postgres, object storage, or embedding providers.
