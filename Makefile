.PHONY: check backend-check

check: backend-check

backend-check:
	cd backend && uv run ruff check .
	cd backend && uv run ruff format --check .
	cd backend && uv run pyright
	cd backend && uv run lint-imports
	cd backend && uv run pytest -q
