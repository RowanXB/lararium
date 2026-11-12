.PHONY: check backend-sync backend-check openapi openapi-check web-install web-check up down backup

COMPOSE = docker compose -f infra/compose.yaml --env-file infra/.env

check: backend-check openapi-check web-check

# Install exactly what the lockfiles pin; fail if a lockfile is out of date.
backend-sync:
	cd backend && uv sync --locked --all-groups

web-install:
	cd web && pnpm install --frozen-lockfile

backend-check: backend-sync
	cd backend && uv run --locked ruff check .
	cd backend && uv run --locked ruff format --check .
	cd backend && uv run --locked pyright
	cd backend && uv run --locked lint-imports
	cd backend && uv run --locked pytest -q

# Regenerate web/openapi.json from the backend, then the typed client from it.
# The export goes to a temporary file first, so a failed export can't truncate the spec.
openapi: backend-sync web-install
	cd backend && uv run --locked python -m lararium.api.export_openapi > ../web/openapi.json.tmp \
		|| { rm -f ../web/openapi.json.tmp; exit 1; }
	mv web/openapi.json.tmp web/openapi.json
	cd web && pnpm openapi-ts

# Fails when the checked-in spec or client is out of date with the backend
# (the second line catches newly generated files that git doesn't track yet).
openapi-check: openapi
	git diff --exit-code -- web/openapi.json web/src/api
	test -z "$$(git ls-files --others --exclude-standard -- web/openapi.json web/src/api)"

web-check: web-install
	cd web && pnpm typecheck
	cd web && pnpm test --run

# Production stack (needs infra/.env; see README).
up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

backup:
	infra/backup/backup.sh
