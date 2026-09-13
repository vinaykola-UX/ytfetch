#!/usr/bin/env bash
# YTFetch — start the server (binds to 0.0.0.0:8000)
cd "$(dirname "$0")"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
