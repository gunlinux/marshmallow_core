#!/usr/bin/env bash
# Dev helper: build the wheel, install it into a throwaway venv against STOCK
# marshmallow (never the fork), and run the suite. Pass extra args to pytest.
set -euo pipefail
cd "$(dirname "$0")"
uvx maturin build --release >/dev/null 2>&1
WHEEL=$(ls -t target/wheels/*.whl | head -1)
uv venv /tmp/mc >/dev/null 2>&1
uv pip install --python /tmp/mc/bin/python --force-reinstall \
    marshmallow pytest "$WHEEL" >/dev/null 2>&1
/tmp/mc/bin/python -m pytest "$@"
