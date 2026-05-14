#!/usr/bin/env bash
# Always uses project .venv so "No module named uvicorn" does not happen.
set -e
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  echo "Creating .venv..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python -m uvicorn app.backend.main:app --reload --host 127.0.0.1 --port 8000
