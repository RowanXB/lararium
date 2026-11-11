.PHONY: check backend-check openapi openapi-check web-check

check: backend-check openapi-check web-check

backend-check:
	cd backend && uv run ruff check .
	cd backend && uv run ruff format --check .
	cd backend && uv run pyright
	cd backend && uv run lint-imports
	cd backend && uv run pytest -q

# Regenerate web/openapi.json from the backend, then the typed client from it.
openapi:
	cd backend && uv run python -m financial_gate.api.export_openapi > ../web/openapi.json
	cd web && pnpm openapi-ts

# Fails when the checked-in spec or client is out of date with the backend
# (the second line catches newly generated files that git doesn't track yet).
openapi-check: openapi
	git diff --exit-code -- web/openapi.json web/src/api
	test -z "$$(git ls-files --others --exclude-standard -- web/openapi.json web/src/api)"

web-check:
	cd web && pnpm typecheck
	cd web && pnpm test --run
