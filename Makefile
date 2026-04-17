# Knowler build system
# Usage:
#   make dev       — run frontend + engine in dev mode (hot reload)
#   make build     — build frontend and bundle into Python package
#   make install   — pip install the package into current venv
#   make release   — build wheel + sdist into engine/dist
#   make check-dist — validate built distributions
#   make publish-testpypi / make publish-pypi

PYTHON ?= python
APP_DIR = app
ENGINE_DIR = engine
STATIC_DIR = $(ENGINE_DIR)/knowler_engine/static

# Ensure Homebrew-installed tools are found
export PATH := /opt/homebrew/bin:/opt/homebrew/sbin:$(PATH)

.PHONY: dev build install release check-dist publish-testpypi publish-pypi test test-e2e e2e-fixture clean

# ─── Development ────────────────────────────────────────────────────────────

dev:
	@echo "Starting engine and frontend..."
	@cd $(ENGINE_DIR) && source .venv/bin/activate && \
	  $(PYTHON) -m knowler_engine serve --dev &
	@cd $(APP_DIR) && npm run dev

test:
	@echo "Running engine test suite..."
	@$(ENGINE_DIR)/.venv/bin/python -m pytest $(ENGINE_DIR)/tests -q

test-e2e:
	@echo "Running RPC end-to-end workflow tests..."
	@$(ENGINE_DIR)/.venv/bin/python -m pytest $(ENGINE_DIR)/tests/test_e2e_rpc_workflow.py -q

e2e-fixture:
	@bash ./scripts/create_e2e_fixture.sh

# ─── Production build ───────────────────────────────────────────────────────

build: build-frontend copy-frontend

build-frontend:
	@echo "Building React frontend..."
	cd $(APP_DIR) && npm ci && npm run build

copy-frontend:
	@echo "Copying frontend into Python package..."
	rm -rf $(STATIC_DIR)
	cp -r $(APP_DIR)/dist $(STATIC_DIR)
	@echo "Frontend bundled at $(STATIC_DIR)"

# ─── Install / release ──────────────────────────────────────────────────────

install: build
	@echo "Installing knowler..."
	cd $(ENGINE_DIR) && pip install -e .

release: build
	@echo "Building source and wheel distributions..."
	cd $(ENGINE_DIR) && $(PYTHON) -m build
	@echo "Distributions at $(ENGINE_DIR)/dist/"

check-dist:
	@echo "Checking built distributions..."
	cd $(ENGINE_DIR) && $(PYTHON) -m twine check dist/*

publish-testpypi: check-dist
	@echo "Uploading distributions to TestPyPI..."
	cd $(ENGINE_DIR) && $(PYTHON) -m twine upload --repository testpypi dist/*

publish-pypi: check-dist
	@echo "Uploading distributions to PyPI..."
	cd $(ENGINE_DIR) && $(PYTHON) -m twine upload dist/*

clean:
	rm -rf $(APP_DIR)/dist $(STATIC_DIR) $(ENGINE_DIR)/dist $(ENGINE_DIR)/*.egg-info
