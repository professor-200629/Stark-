#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
python3 -m app.seed
exec python3 -m uvicorn app.main:app --host "${STARK_HOST:-127.0.0.1}" --port "${STARK_PORT:-8000}"
