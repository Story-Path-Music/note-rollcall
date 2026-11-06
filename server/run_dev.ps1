# Dev server on http://localhost:8787 (DEV_AUTH=1, mock-safe defaults). Env vars already set are kept.
# Upstream-linked mode: set WP_BASE_URL and SPM_TOOLS_AUTH_SECRET in the environment first.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $env:DEV_AUTH) { $env:DEV_AUTH = '1' }
if (-not $env:APP_BASE_URL) { $env:APP_BASE_URL = 'http://localhost:8787' }
if (-not $env:OMR_ENGINE) { $env:OMR_ENGINE = '' }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $py -m uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8787
