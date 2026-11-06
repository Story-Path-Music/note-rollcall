"""Cloud Run page worker (WORKER_MODE=1): stateless, synchronous, one page per request.

Only GET /healthz and POST /api/page exist.  Recognition runs inside the request (Cloud Run request-based billing
charges CPU only while a request is open); nothing is kept after the response: the page lives in a TemporaryDirectory.  Auth: X-SPM-Worker-Key, constant-time compared to WORKER_KEY.
"""
from __future__ import annotations

import hmac
import logging
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from . import pipeline
from .config import Settings

log = logging.getLogger("spm.worker")


class WorkerError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def create_worker_app(s: Settings) -> FastAPI:
    app = FastAPI(title="spm-note-worker", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = s
    busy = threading.Semaphore(1)  # one page at a time per instance (Cloud Run concurrency=1 also enforces it)
    limit = s.worker_max_image_mb * 1024 * 1024
    key = s.worker_key.encode("utf-8")

    @app.exception_handler(WorkerError)
    async def _err(request, exc: WorkerError):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mode": "worker"}

    @app.post("/api/page")
    def api_page(request: Request, file: UploadFile = File(...), engine: str | None = Form(None),
                 index: int = Form(0), pt_w: float | None = Form(None), pt_h: float | None = Form(None)):
        if not hmac.compare_digest(request.headers.get("x-spm-worker-key", "").encode("utf-8"), key):
            raise WorkerError(401, "unauthorized")
        eng = (engine or s.omr_engine).strip()
        if eng not in s.engine_names():
            raise WorkerError(400, "unknown_engine")
        if not 0 <= index < 10_000:
            raise WorkerError(400, "bad_index")
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit + 100_000:
            raise WorkerError(413, "file_too_large")
        pg = None
        if pt_w is not None and pt_h is not None and pt_w > 0 and pt_h > 0:
            pg = {"pt_w": pt_w, "pt_h": pt_h}
        with tempfile.TemporaryDirectory(prefix="page-") as td:
            png, out = Path(td) / "page.png", Path(td) / "page.json"
            size = 0
            with open(png, "wb") as f:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise WorkerError(413, "file_too_large")
                    f.write(chunk)
            if size == 0:
                raise WorkerError(422, "empty_file")
            with open(png, "rb") as f:
                if pipeline.sniff(f.read(16)) != "png":  # exactly one raster page; PDFs/multi-page are not accepted
                    raise WorkerError(415, "png_only")
            try:
                with Image.open(png) as im:
                    im.verify()
                with Image.open(png) as im:
                    wh = im.size
            except Exception:  # noqa: BLE001
                raise WorkerError(422, "invalid_image")
            if wh[0] * wh[1] > pipeline.MAX_PIXELS:
                raise WorkerError(413, "image_too_large")
            if pg is not None:
                pg.update(w=wh[0], h=wh[1])
            with busy:
                try:
                    raw = pipeline.run_engine(s, eng, png, out)
                except RuntimeError as e:
                    msg = str(e)
                    log.warning("page %s failed: %s", index, msg[:200])
                    if msg.startswith(("engine_failed", "engine_bad_output")):  # deterministic: local would fail too
                        raise WorkerError(422, msg[:300])
                    if msg == "engine_timeout":
                        raise WorkerError(504, msg)
                    raise WorkerError(500, "internal_error")
            return pipeline.merge_page(index, raw, pg, wh)

    return app
