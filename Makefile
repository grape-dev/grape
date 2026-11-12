# Makefile — Grape unified build entry point
#
# Usage:
#   make help    - show available targets
#   make install - install package in editable mode (core deps)
#   make install-dev - install with dev dependencies
#   make test    - run pytest test suite
#   make run     - start the API server (dev mode, port 3000)
#   make sync-skills - sync official skills from grape-skills repo
#   make lint    - run basic import/syntax checks
#   make clean   - remove build artifacts and caches
#   make docker  - build and run via docker-compose (from deploy/)

PYTHON ?= $(shell [ -x venv/bin/python ] && echo venv/bin/python || echo python3)
PORT ?= 3000
VENV ?= .venv
# 测试解释器：优先仓库自带的 venv/ 或 .venv/（本仓库部署用 venv/），否则退回 PYTHON
TEST_PYTHON ?= $(firstword $(wildcard venv/bin/python .venv/bin/python) $(PYTHON))
# 逐域测试的领域顺序（可覆盖：make test-domains DOMAINS="kernel api"）
DOMAINS ?= arch atomic contracts kernel insight api mgmt runtime

.PHONY: help install install-dev install-all test test-fast run sync-skills lint clean docker

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install package in editable mode (core deps only)
	$(PYTHON) -m pip install -e .

install-dev: ## Install with dev dependencies (pytest, playwright, etc.)
	$(PYTHON) -m pip install -e ".[dev]"

install-all: ## Install with all optional dependencies (mysql, obs, im, dev)
	$(PYTHON) -m pip install -e ".[all]"

install-worker: ## Install worker-only dependencies
	$(PYTHON) -m pip install -e ".[worker]"

test: ## Run pytest test suite
	$(PYTHON) -m pytest tests/ -v

test-domains: ## Run tests per domain in isolated processes (stable order; see tests/README.md)
	@set -e; for d in $(DOMAINS); do \
		echo "== tests/$$d =="; \
		$(TEST_PYTHON) -m pytest tests/$$d -q || exit 1; \
	done

test-fast: ## Run pytest (fast: skip slow/e2e markers)
	$(PYTHON) -m pytest tests/ -v -m "not e2e_ui and not atomic"

run: ## Start the API server (dev mode)
	$(PYTHON) start_server.py

sync-skills: ## Sync official skills from grape-skills repo
	bash scripts/sync_skills.sh

lint: ## Basic import/syntax check
	$(PYTHON) -c "import src.api.main; print('import OK')"

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

docker: ## Build and run via docker-compose (from deploy/)
	docker compose -f deploy/docker-compose.yml up --build
