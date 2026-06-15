# marshmallow_core — mixed Python/Rust project (maturin).
# Quality + test targets for both languages.

.DEFAULT_GOAL := help

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

.PHONY: dev
dev:  ## Install dev dependencies
	uv sync --dev

.PHONY: develop
develop:  ## Build the Rust core and install into the venv (iterating)
	uvx maturin develop --release
	@# ld-1328.2 (macOS 15) produces 4-byte-aligned LINKEDIT string pools that
	@# dyld rejects. Patch in-place when the file exists; no-op on other systems.
	@python3 scripts/fix_linkedit.py -v python/marshmallow_core/_core.abi3.so 2>/dev/null || true

# ---- combined gates -------------------------------------------------------

.PHONY: check
check: lint types test  ## Run all quality gates + tests (Python + Rust)

.PHONY: lint
lint: py-lint rust-lint  ## Lint Python and Rust

.PHONY: fix
fix: py-fix rust-fix  ## Auto-fix/format Python and Rust

.PHONY: test
test: py-test rust-test  ## Run Python and Rust tests

# ---- Python ---------------------------------------------------------------

.PHONY: py-lint
py-lint:  ## Lint Python (ruff)
	uv run ruff check
	uv run ruff format --check

.PHONY: py-fix
py-fix:  ## Fix + format Python (ruff)
	uv run ruff check --fix
	uv run ruff format

.PHONY: types
types:  ## Type-check Python (pyright)
	uv run pyright

.PHONY: py-test
py-test: develop  ## Run Python tests (rebuilds the core first)
	uv run pytest

# ---- Rust -----------------------------------------------------------------

.PHONY: rust-lint
rust-lint:  ## Lint Rust (clippy, warnings = errors) + format check
	cargo clippy --all-targets -- -D warnings
	cargo fmt --check

.PHONY: rust-fix
rust-fix:  ## Format Rust + apply clippy fixes
	cargo fmt
	cargo clippy --fix --allow-dirty --allow-staged --all-targets

.PHONY: rust-test
rust-test:  ## Run Rust tests
	cargo test
