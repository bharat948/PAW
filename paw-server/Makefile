.PHONY: install lint typecheck test test-integration check migrate run worker up down dev-infra dev-infra-down
install:
	uv sync --frozen
lint:
	uv run ruff check . && uv run ruff format --check .
typecheck:
	uv run mypy
test:
	uv run pytest -m "not integration and not live"
test-integration: dev-infra
	PAW_TEST_DATABASE_URL=postgresql://paw:paw_local_only@localhost:5432/paw uv run pytest -m "integration and not live"
check: lint typecheck test
migrate:
	uv run alembic -c alembic.ini upgrade head
run:  # API with hot reload on :8000 (reads .env)
	uv run uvicorn --factory paw_server.main:app_factory --reload --port 8000
worker:  # ingestion worker, restarts on code changes
	uv run watchfiles --filter python 'python -m paw_server.worker' src
dev-infra:  # Postgres + S3 in Docker, published on localhost; app code runs on the host
	docker compose stop api worker web >/dev/null 2>&1 || true
	docker compose -f compose.yaml -f compose.dev.yaml up -d --wait db s3
dev-infra-down:
	docker compose -f compose.yaml -f compose.dev.yaml stop db s3
up:
	docker compose up --build -d
down:
	docker compose down
