#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/backend"
echo "Recovery Console"
echo "----------------"
python run_eval.py --size 500
echo
echo "Starting console at http://127.0.0.1:8000"
exec uvicorn app.main:app --reload --port 8000
