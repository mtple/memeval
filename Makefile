# Market Replay — local-first commands. Equivalent direct commands are listed in README.md.
SHELL := /bin/bash
UV ?= uv
PY := .venv/bin/python
MR := .venv/bin/market-replay
PNPM ?= pnpm
DATA ?= data

.PHONY: setup test test-fast test-browser demo verify build serve import-report-fixtures collect validate-pack run-agent export-run fixtures lint schemas clean

setup: ## Create the venv, install pinned Python deps, install web deps
	$(UV) venv --python 3.12 .venv
	$(UV) sync --extra dev
	$(PNPM) install --frozen-lockfile

fixtures: ## Generate the shipped generated suite (four weeks + dev fixture) into $(DATA)/packs/generated
	$(MR) fixtures generate --out $(DATA)/packs/generated

test: ## Unit, property, integration and security tests (no network, no keys)
	$(PY) -m pytest tests -m "not browser" -p no:cacheprovider

test-fast:
	$(PY) -m pytest tests/unit tests/property -p no:cacheprovider -q

test-browser: ## Playwright smoke tests against the built UI (needs `make build`)
	$(PY) -m pytest tests/browser -m browser -p no:cacheprovider -q

demo: ## No-key demo: generate four full weeks, run three reference participants, compare, export
	$(MR) demo --data-dir $(DATA)

demo-quick:
	$(MR) demo --quick --data-dir $(DATA)

verify: test ## Tests plus the environment-validation report with sensitivity runs
	$(MR) verify --data-dir $(DATA)

build: ## Build the web UI (served by `make serve` at /)
	$(PNPM) --filter web build

serve: ## Start the HTTP service on 127.0.0.1:8000 (prints the admin token)
	$(MR) serve --data-dir $(DATA)

import-report-fixtures: ## Build and import the diagnostic-only pack from the supplied report excerpts
	$(MR) import-report-fixtures --data-dir $(DATA)

collect: ## Opt-in read-only historical collection: make collect ARGS="--config path/to/authorized-collection.yaml"
	$(MR) collect $(ARGS) --data-dir $(DATA)

validate-pack: ## make validate-pack PACK=path/to/pack
	$(MR) packs validate $(PACK)

run-agent: ## make run-agent ARGS="--agent cash_only --suite generated-practice-v1"
	$(MR) run-agent $(ARGS) --data-dir $(DATA)

export-run: ## make export-run RUN=run_id
	$(MR) export-run $(RUN) --data-dir $(DATA)

lint:
	.venv/bin/ruff check src tests sdk agents
	.venv/bin/ruff format --check src tests sdk agents || true

schemas: ## Export OpenAPI and JSON Schemas to schemas/
	$(PY) scripts/export_schemas.py

clean:
	rm -rf .pytest_cache .ruff_cache tests/browser/output
