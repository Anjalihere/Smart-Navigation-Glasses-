#!/usr/bin/env bash
# Prefer an existing project virtualenv so "No module named uvicorn" does not happen.
set -e
cd "$(dirname "$0")"

VENV_DIR=""
for candidate in .venv venv venc; do
  if [[ -d "$candidate" ]]; then
    VENV_DIR="$candidate"
    break
  fi
done

if [[ -z "$VENV_DIR" ]]; then
  echo "Creating .venv..."
  python3 -m venv .venv
  VENV_DIR=".venv"
fi

"$VENV_DIR/bin/python" -m pip install -q -r requirements.txt
exec "$VENV_DIR/bin/python" -m uvicorn app.backend.main:app --reload --reload-exclude '.venv/*' --reload-exclude 'venv/*' --reload-exclude 'venc/*' --host 127.0.0.1 --port 8000
