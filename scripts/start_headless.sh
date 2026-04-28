#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR/backend"

if [[ ! -d ".venv" ]]; then
  echo "Missing backend/.venv. Create it first."
  exit 1
fi

exec .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
