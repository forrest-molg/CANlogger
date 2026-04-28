#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

docker compose up --build -d

echo "CANlogger started"
echo "GUI: http://localhost:8000"
echo "API docs: http://localhost:8000/docs"
