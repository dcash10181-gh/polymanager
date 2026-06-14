# Polymanager — one-command setup and common tasks.
# Run `make` (or `make help`) to see targets.

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip

.DEFAULT_GOAL := help

.PHONY: help setup install test demo run report clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: install test ## One-command setup: create venv, install deps, run tests
	@echo ""
	@echo "Setup complete. Next:  make demo   (offline, no funds/keys)"
	@echo "Then on your own machine:  cp .env.example .env  &&  make run"

$(VENV): ## Create the virtualenv
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV) ## Install dependencies into the venv
	$(PIP) install -r requirements.txt

test: ## Run the test suite
	$(PY) -m pytest -q

demo: ## Offline paper-mode demo (no funds, no keys, no network)
	$(PY) -m scripts.demo_paper

run: ## Run the bot (paper mode unless BOT_MODE is set in .env)
	$(PY) -m app.main

report: ## Print the performance report from the audit log
	$(PY) -m app.report

clean: ## Remove venv, caches, and local run data
	rm -rf $(VENV) .pytest_cache **/__pycache__ data/*.db logs/*.jsonl
