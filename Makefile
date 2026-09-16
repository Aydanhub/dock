.PHONY: help install dev run test test-cov eval eval-update lint fmt typecheck check demo docker-build docker-run clean

VENV := ./.venv/bin

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Create a venv and install the project with dev dependencies
	python3 -m venv .venv
	$(VENV)/pip install --upgrade pip
	$(VENV)/pip install -e ".[dev]"

dev:  ## Run with auto-reload at http://localhost:8000/docs
	$(VENV)/uvicorn app.main:app --reload

run:  ## Run without reload, bound to all interfaces
	$(VENV)/uvicorn app.main:app --host 0.0.0.0 --port 8000

test:  ## Run the test suite
	$(VENV)/pytest -q

test-cov:  ## Run tests with a coverage report and an 85% floor
	$(VENV)/pytest -q --cov=app --cov-report=term-missing --cov-fail-under=85

eval:  ## Check the model's decisions against the labelled regression set
	$(VENV)/python -m app.evals.runner --report eval_reports/latest.json

eval-update:  ## Record the current scores as the new drift baseline
	$(VENV)/python -m app.evals.runner --update-baseline

lint:  ## Lint
	$(VENV)/ruff check .

fmt:  ## Fix what the linter can fix
	$(VENV)/ruff check --fix .

typecheck:  ## Type-check under mypy strict
	$(VENV)/mypy app

check: lint typecheck test-cov eval  ## Everything CI runs, in one command

demo:  ## Start a real server and walk through every feature
	$(VENV)/python demo/run_demo.py

docker-build:  ## Build the production image
	docker build -t dock:local .

docker-run:  ## Run the stack with docker compose
	docker compose up --build

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage eval_reports
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
