# mini-erp -- Week 7 hardening brief, Decision 4.
#
# Quick start:
#   make up        # docker compose up, blocks until the app is healthy
#   make bootstrap # create initial admin user & company (Rule 10b)
#   make seed      # populate a realistic demo dataset (idempotent, opt-in)
#   make demo      # walk one order through the live API, print the ledger
#   make check     # the same ruff/mypy/lint-imports/pytest sequence CI runs

.PHONY: up down bootstrap seed demo test check clean

APP_PORT ?= 8000

up:
	docker compose up -d --build --wait

down:
	docker compose down

bootstrap:
	@if [ -z "$$BOOTSTRAP_ADMIN_EMAIL" ] || [ -z "$$BOOTSTRAP_ADMIN_PASSWORD" ]; then \
		echo "ERROR: BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD must be set." >&2; \
		exit 1; \
	fi
	docker compose exec -T -e BOOTSTRAP_ADMIN_EMAIL -e BOOTSTRAP_ADMIN_PASSWORD -e BOOTSTRAP_COMPANY_CODE -e BOOTSTRAP_COMPANY_NAME -e BOOTSTRAP_ADMIN_COMPANY_CODE -e BOOTSTRAP_ADMIN_COMPANY_NAME app python -m app.cli.bootstrap_admin

# Runs INSIDE the already-migrated `app` container (`docker compose exec`),
# not from the host -- `app.cli.seed_demo` talks to the database directly
# (not over HTTP) and needs the container's own DATABASE_URL, which points
# at the `postgres` service by its docker-network hostname. `-T` disables
# pseudo-TTY allocation so this is safe to run non-interactively (CI, this
# Makefile).
seed:
	docker compose exec -T -e ALLOW_SEED_DEMO_IN_PRODUCTION -e BOOTSTRAP_COMPANY_CODE -e BOOTSTRAP_ADMIN_COMPANY_CODE app python -m app.cli.seed_demo

# Runs from the HOST against the container's published port -- unlike
# `seed`, `app.cli.demo_o2c` is a real HTTP client proving the live REST
# API works, so it belongs outside the container, hitting the same port a
# `curl` walkthrough would.
demo:
	@if [ -z "$${BOOTSTRAP_ADMIN_EMAIL:-$$DEMO_ADMIN_EMAIL}" ] || [ -z "$${BOOTSTRAP_ADMIN_PASSWORD:-$$DEMO_ADMIN_PASSWORD}" ]; then \
		echo "ERROR: BOOTSTRAP_ADMIN_EMAIL/PASSWORD (or DEMO_ADMIN_EMAIL/PASSWORD) must be set." >&2; \
		exit 1; \
	fi
	DEMO_BASE_URL=http://localhost:$(APP_PORT) \
	BOOTSTRAP_ADMIN_EMAIL="$${BOOTSTRAP_ADMIN_EMAIL:-$${DEMO_ADMIN_EMAIL}}" \
	BOOTSTRAP_ADMIN_PASSWORD="$${BOOTSTRAP_ADMIN_PASSWORD:-$${DEMO_ADMIN_PASSWORD}}" \
	uv run python -m app.cli.demo_o2c

test:
	uv run pytest

# The exact sequence this project's CI and every Codex diff review this
# week rely on locally -- ruff check, ruff format --check, mypy, import
# boundary contracts, then the full test suite.
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run python -m mypy .
	uv run lint-imports
	uv run pytest

clean:
	docker compose down -v
