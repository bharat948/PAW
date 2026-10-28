.PHONY: install lint typecheck test test-integration check migrate run up down
install:
	uv sync --frozen
lint:
	uv run ruff check . && uv run ruff format --check .
typecheck:
	uv run mypy
test:
	uv run pytest -m "not integration"
test-integration:  # needs: docker compose -f compose.yaml -f compose.dev-db.yaml up -d db
	PAW_TEST_DATABASE_URL=postgresql://paw:paw_local_only@localhost:54329/paw uv run pytest -m integration
check: lint typecheck test
migrate:
	uv run alembic -c alembic.ini upgrade head
run:
	uv run uvicorn --factory paw_server.main:app_factory --reload
up:
	docker compose up --build -d
down:
	docker compose down
