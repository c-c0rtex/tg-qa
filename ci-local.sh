#!/usr/bin/env bash
# Local mirror of CI: run before finalizing any change.
set -euo pipefail
cd "$(dirname "$0")"

echo "== ruff =="
uv run ruff check .

echo "== pytest (unit) =="
uv run pytest -q

echo "OK"
