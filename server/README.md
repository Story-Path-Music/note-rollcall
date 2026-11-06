# server

FastAPI backend implementing `docs/CONTRACT.md` sections 2, 3 and 5. If `../web/index.html` exists it is served at `/` (no-store); this repository only ships `web/fonts/`.

## Setup

```
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python
```

## Run

- Dev (no upstream site): `./run_dev.sh` or `powershell -File run_dev.ps1` -> http://localhost:8787
  Sign in with `/auth/dev-login?name=tester&quota=3` (only when `DEV_AUTH=1`).
- Upstream-linked: set `WP_BASE_URL` (the upstream site that issues handoff tokens) and `SPM_TOOLS_AUTH_SECRET` (shared HMAC secret) before starting; `/auth/login?next=/` then redirects there. Leave `DEV_AUTH` off in production.
- Tests: `.venv/bin/python -m pytest -q tests`

## Environment variables

| Name | Default | Notes |
|---|---|---|
| `APP_BASE_URL` | `http://localhost:8787` | used to build the handoff `return` URL; `https` makes cookies `Secure` |
| `WP_BASE_URL` | empty | upstream site root |
| `SPM_TOOLS_AUTH_SECRET` | empty | shared HMAC secret; random per-start in `DEV_AUTH` mode if unset |
| `SERVICE_KEY` | empty | non-empty = service mode (requests must carry the key; see `docs/CONTRACT.md` section 3), at least 16 characters |
| `DEV_AUTH` | off | `1` enables `/auth/dev-login` |
| `DEV_DAILY_QUOTA` | 5 | dev-login default quota |
| `MAX_UPLOAD_MB` | 30 | 413 above |
| `MAX_PAGES` | 10 | 422 above |
| `OMR_ENGINE` | `default` from `omr/engines.json`, else `mock` | |
| `OMR_TIMEOUT_PER_PAGE_S` | 600 | per engine subprocess |
| `OMR_CONCURRENCY` | 1 (8 when `REMOTE_WORKER_URL` set) | concurrent jobs |
| `WORKER_MODE`, `WORKER_KEY`, `REMOTE_WORKER_URL`, `REMOTE_MAX_PARALLEL`, `REMOTE_TIMEOUT_S`, `WORKER_MAX_IMAGE_MB` | off | optional remote page worker / dispatcher, see `docs/CONTRACT.md` section 5 |
| `DATA_DIR` | `server/data` | SQLite (`app.sqlite`) + job dirs; gitignored |
| `JOB_RETENTION_HOURS` | 24 | cleanup on startup and hourly |
| `SESSION_SECRET` | random per start | cookie signing |
| `ADMIN_TOKEN` | unset (endpoint disabled) | `GET /admin/usage?token=...` or header `X-Admin-Token` |

Secrets are never logged. Set `SPM_TOOLS_AUTH_SECRET`, `SESSION_SECRET`, `SERVICE_KEY` and `ADMIN_TOKEN` to random strings in your shell or an untracked env file; never commit them.

## Notes

- Engines: `omr/run_engine.py` and `omr/engines.json` are used when present; otherwise `app/mock_engine.py` (same CLI). Mock test knobs: `MOCK_ENGINE_DELAY_MS`, `MOCK_ENGINE_FAIL=1`.
- Quota: a row in `usage` is reserved at submission (status `reserved`), becomes `done` on success and `failed` (not counted) on error. Same user + same day (Asia/Taipei) + same sha256 returns the cached job.
- Export: PDF sources are re-opened from the original upload (vector content and page size kept); label font size is px -> pt via `pdf_width_pt / width_px`. Image sources draw on the EXIF-normalized page PNG.
- Page `width_px` can differ from `pt*dpi/72` by 1 px (pixmap rounding); export uses the exact reported ratio.
