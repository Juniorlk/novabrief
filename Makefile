# NovaBrief — drives the three toolchains of the monorepo.
# Usage: make lint | make test | make fmt | make poc1
#
# On Windows, run from Git Bash. GNU Make is required
# (`winget install ezwinports.make`).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# pnpm ships with Node through corepack: no global install needed.
PNPM ?= corepack pnpm
PY   ?= python

.PHONY: help setup fmt fmt-check lint lint-rust lint-python lint-node \
        test test-rust test-python test-node poc1 clean

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Development tooling
# ---------------------------------------------------------------------------
setup: ## Install the tooling for all three languages
	rustup show
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	$(PNPM) install

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
fmt: ## Format Rust, Python and TypeScript
	cargo fmt --all
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix-only .
	$(PNPM) run format

fmt-check: ## Check formatting without modifying anything
	cargo fmt --all -- --check
	$(PY) -m ruff format --check .
	$(PNPM) run format:check

# ---------------------------------------------------------------------------
# Lint — CLAUDE.md section 6: ruff, mypy --strict, clippy -D warnings, eslint
# ---------------------------------------------------------------------------
lint: lint-rust lint-python lint-node ## Lint all three languages

lint-rust: ## cargo fmt --check + clippy -D warnings
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets --all-features -- -D warnings

lint-python: ## ruff (lint + format) + mypy --strict
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy

lint-node: ## eslint (no warning tolerated) + tsc
	$(PNPM) run lint
	$(PNPM) run typecheck

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
test: test-rust test-python test-node ## Run the tests of all three languages

test-rust: ## cargo test over the workspace
	cargo test --workspace --all-features

test-python: ## pytest; exit code 5 (nothing collected) is tolerated for now
	@set +e; $(PY) -m pytest; status=$$?; set -e; \
	if [ $$status -eq 5 ]; then \
		echo "pytest: no test collected yet - the toolchain itself works."; \
	else \
		exit $$status; \
	fi

test-node: ## vitest (--passWithNoTests while no package exists yet)
	$(PNPM) run test

# ---------------------------------------------------------------------------
# POC #1 — Windows audio capture (docs/tasks/01_POC1_capture_audio.md)
# ---------------------------------------------------------------------------
poc1: ## Build and run nb-capture (Windows only)
	cargo build --release -p nb-capture -p nb-testsignal
	cargo run --release -p nb-capture -- $(ARGS)

clean: ## Remove build artefacts and tool caches
	cargo clean
	rm -rf .mypy_cache .ruff_cache .pytest_cache
