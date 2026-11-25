.PHONY: check backend-sync backend-check license-check real-check openapi openapi-check web-install web-check up down backup restore-rehearsal

COMPOSE = docker compose -f infra/compose.yaml --env-file infra/.env

check: backend-check license-check openapi-check web-check

# Install exactly what the lockfiles pin; fail if a lockfile is out of date.
backend-sync:
	cd backend && uv sync --locked --all-groups

web-install:
	cd web && pnpm install --frozen-lockfile

backend-check: backend-sync
	cd backend && uv run --locked ruff check .
	cd backend && uv run --locked ruff format --check .
	cd backend && uv run --locked ruff check --config pyproject.toml ../tools
	cd backend && uv run --locked ruff format --check --config pyproject.toml ../tools
	cd backend && uv run --locked pyright
	cd backend && uv run --locked lint-imports
	cd backend && uv run --locked pytest -q

# Every production dependency's licence must be compatible with AGPL-3.0-or-later, or have a
# reviewed exception (tools/licenses.toml; README, "Dependency licences").
license-check: backend-sync web-install
	cd backend && uv run --locked python ../tools/license_check.py

# Your own bank statements, end to end on the throwaway test database (README, "Verifying
# against your real statements"): make real-check REAL_ROOT=<dir holding HSBC/Lloy/BOC/Revolut/Tembo/LemF folders>,
# and/or ETORO_STATEMENT=<an eToro account statement, xlsx> (at least one of the two).
# Prints counts and pass or fail only.
real-check: backend-sync
	$(if $(REAL_ROOT)$(ETORO_STATEMENT),,$(error set REAL_ROOT to the folder holding one folder per bank or ETORO_STATEMENT to an eToro statement (xlsx) or both))
ifneq ($(REAL_ROOT),)
	root="$$(cd "$(REAL_ROOT)" && pwd)" && cd backend && LARARIUM_REAL_ROOT="$$root" \
		uv run --locked pytest -q -s -p no:cacheprovider tests/finance/importing/test_real_files.py
endif
ifneq ($(ETORO_STATEMENT),)
	test -f "$(ETORO_STATEMENT)"
	statement="$$(cd "$$(dirname "$(ETORO_STATEMENT)")" && pwd)/$$(basename "$(ETORO_STATEMENT)")" \
		&& cd backend && LARARIUM_ETORO_STATEMENT="$$statement" \
		uv run --locked pytest -q -s -p no:cacheprovider tests/finance/etoro/test_real_statement.py::test_real_statement
endif

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

# Every profile, so services started under one since turned off (the webhook receiver and
# tunnel after COMPOSE_PROFILES is cleared) are stopped too.
down:
	$(COMPOSE) --profile '*' down

backup:
	infra/backup/backup.sh

# Restore a backup into a throwaway PostgreSQL on another Docker host and check it (README,
# "Restore rehearsal"): make restore-rehearsal CONTEXT=<docker context> DUMP=<.dump> DOCUMENTS=<.tar>
restore-rehearsal:
	$(if $(and $(CONTEXT),$(DUMP),$(DOCUMENTS)),,$(error set CONTEXT, DUMP and DOCUMENTS))
	infra/backup/rehearse-restore.sh "$(CONTEXT)" "$(DUMP)" "$(DOCUMENTS)"
