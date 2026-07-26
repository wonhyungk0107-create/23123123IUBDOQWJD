# Canonical commands. Every target is a single uv invocation so it behaves the same
# on Windows (where recipes run through cmd) as on Linux and macOS.
#
# If you do not have GNU make, run the command shown under each target directly.

.DEFAULT_GOAL := help
.PHONY: help bootstrap format lint typecheck test coverage migrate migrate-down demo verify docker-check live-smoke clean

UV ?= uv

help:
	@echo "bootstrap     install the project and dev dependencies (clean checkout)"
	@echo "format        format with ruff"
	@echo "lint          ruff check"
	@echo "typecheck     mypy --strict"
	@echo "test          pytest"
	@echo "coverage      pytest with coverage + per-module floors"
	@echo "migrate       apply alembic migrations to head"
	@echo "migrate-down  roll migrations back to base"
	@echo "demo          deterministic offline end-to-end run"
	@echo "verify        format check, lint, typecheck, coverage, demo"
	@echo "docker-check  build the image and run the demo inside it"
	@echo "live-smoke    opt-in read-only live checks; skips without credentials"

bootstrap:
	$(UV) sync --extra dev

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint:
	$(UV) run ruff format --check .
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy

test:
	$(UV) run pytest

coverage:
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=json
	$(UV) run python tools/check_coverage.py

migrate:
	$(UV) run alembic upgrade head

migrate-down:
	$(UV) run alembic downgrade base

demo:
	$(UV) run tradeup demo

# The mandatory quality gate. Runs the same checks CI runs, in the same order.
verify: lint typecheck coverage demo
	@echo "verify: all gates passed"

docker-check:
	docker build -t tradeup:local .
	docker run --rm tradeup:local tradeup demo --quiet

# Read-only, opt-in, and skips cleanly when credentials are absent.
live-smoke:
	$(UV) run pytest -m live -q

clean:
	$(UV) run python -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.pytest_cache','.mypy_cache','.ruff_cache','.hypothesis','htmlcov']]; [pathlib.Path(f).unlink(missing_ok=True) for f in ['coverage.json','.coverage','tradeup.db']]"
