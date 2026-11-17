# PAW

PAW is a personal agent workspace for ingesting documents, finding source passages, and working with an Admin Hermès agent. The FastAPI server and background workers use PostgreSQL with pgvector for application state and SeaweedFS for local S3-compatible document storage. The web app uses React, TypeScript, and Vite.

## Quick start

Start Docker with the Compose v2 plugin. From a fresh checkout, run:

```bash
git clone https://github.com/bharat948/PAW.git
cd PAW
docker compose -f paw-server/compose.yaml up --build -d
```

Open [PAW](http://localhost:8080) and sign in with the local-only token `paw_local_only_owner_token`. Compose runs database migrations before starting the API and ingestion worker. The default local embedding and answer providers use test implementations and require no API key.

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/ready
```

`/health` confirms the API process is responding. `/ready` confirms PostgreSQL is reachable and the database schema is current. If readiness fails after startup, inspect the service status and logs from the repository root:

```bash
docker compose -f paw-server/compose.yaml ps -a
docker compose -f paw-server/compose.yaml logs --tail=50 migrate api
```

Upload a small text or Markdown file in **Documents**, wait for its status to reach **Completed**, then look for a phrase from the file in **Search** and open its source. Stop the stack with `docker compose -f paw-server/compose.yaml down`. The database and document volumes persist.

## Features on main

| Feature | What you can do |
| --- | --- |
| Documents | Upload text-based PDF, text, Markdown, CSV, TSV, JSON, or JSONL. Track background processing and retrieve original files. |
| Search and Ask | Run hybrid keyword and vector search, inspect source passages, and stream grounded answers with citations. Configure a real provider for model-generated answers. |
| Admin Hermès | With the optional runtime configured, run streaming agent chats, stop runs, and inspect tool outcomes. The versioned actor profile controls the model, permitted tools, and run limits. |
| Google integration | Configure Google sign-in for the home stack. Connect Google Calendar to read upcoming events through the Integrations page and, when granted, through the Hermès gateway. |
| Home deployment | Run the foundation stack with the home Compose file, reverse proxy, and persistent volumes. Follow the environment contract for provisioning, authentication, and migration. |

The Docker quick start runs the document workspace. To enable the agent runtime, follow the [Hermès gateway setup](deploy/hermes/README.md#6-gateway-end-to-end-002d). The [server guide](paw-server/README.md#using-openai-embeddings) covers model and embedding provider configuration.

## Local development

Use Docker Compose v2, Bash, Make, Python 3.12, `uv`, and Node 20.20.2 from [the web app's version file](paw-web/.nvmrc).

```bash
HERMES=0 ./dev.sh    # Start Postgres and SeaweedFS in Docker and the app with hot reload
./dev.sh stop        # Stop the development infrastructure
```

The web app runs at [localhost:5173](http://localhost:5173) and the API docs at [localhost:8000/docs](http://localhost:8000/docs). The script creates local `.env` files from the examples on first run. Sign in with `PAW_OWNER_TOKEN` from `paw-server/.env`.

- [Server setup and tests](paw-server/README.md)
- [Web setup and tests](paw-web/README.md)
- [Operations runbook](docs/operations.md)
- [Home environment contract](docs/environment-contract.md) and [Google sign-in setup](docs/auth-google-setup.md)
