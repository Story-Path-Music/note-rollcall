#!/usr/bin/env bash
# Dev server on http://localhost:8787 (DEV_AUTH=1, mock-safe defaults). Env vars already set are kept.
# Upstream-linked mode: export WP_BASE_URL and SPM_TOOLS_AUTH_SECRET first.
cd "$(dirname "$0")" || exit 1
export DEV_AUTH="${DEV_AUTH:-1}"
export APP_BASE_URL="${APP_BASE_URL:-http://localhost:8787}"
if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
exec "$PY" -m uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8787
