#!/usr/bin/env bash
# Start the anonymous review API. Dependencies live in ./.pylibs (no system
# site-packages modification required).
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="/workspace/.pylibs/lib/python3.11/site-packages:${PYTHONPATH:-}"
export REVIEW_DB_PATH="${REVIEW_DB_PATH:-data/review.db}"

exec python3 -m uvicorn review_system.main:app --host 127.0.0.1 --port 8000 "$@"
