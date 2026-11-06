"""FastAPI app: auth, quota, OMR jobs, export. See docs/CONTRACT.md."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import shutil
import uuid
from typing import Literal
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from . import clock, export as exp, pipeline, tokens
from .config import Settings
from .jobs import JobManager
from .store import Store

log = logging.getLogger("spm.app")
COOKIE = "spm_session"
SESSION_TTL = 12 * 3600
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, **extra):
        self.status, self.body = status, {"error": code, **extra}


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-store"
        for h in ("etag", "last-modified"):
            if h in r.headers:
                del r.headers[h]
        return r


class LabelPart(BaseModel):
    """標記的一段：文字（x = 中心）或升降記號字形（x = 左緣）；x／size／dy 以該標記字級為單位。"""
    kind: Literal["text", "acc"]
    text: str = Field("", max_length=12)
    name: Literal["flat", "natural", "sharp", "doubleSharp", "doubleFlat"] | None = None
    x: float = Field(0, ge=-20, le=20)
    size: float = Field(0, ge=0, le=4)
    dy: float = Field(0, ge=-4, le=4)


class Label(BaseModel):
    page: int = 0
    x: float
    y: float
    text: str = ""
    dots_above: int = 0
    dots_below: int = 0
    parts: list[LabelPart] | None = Field(None, max_length=4)  # 選填：含升降記號時的分段排版（缺省畫整串 text）
    font_size_px: float | None = Field(None, gt=0, le=2000)  # 選填：此標記自己的字級（局部縮小的列）；缺省用 style.font_size_px


class Style(BaseModel):
    font_size_px: float = Field(28, gt=0, le=2000)
    color: str = "#d0021b"


class ExportReq(BaseModel):
    job_id: str
    format: str = "pdf"
    page: int | None = None
    style: Style = Style()
    labels: list[Label] = Field(default_factory=list, max_length=50000)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    if s.worker_mode:  # Cloud Run 單頁 worker：無 job store、無登入、無靜態檔
        from .worker import create_worker_app
        return create_worker_app(s)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(s.data_dir / "app.sqlite")
    jobs = JobManager(s, store)
    ser = URLSafeTimedSerializer(s.session_secret, salt="spm-session")
    secure_cookie = s.app_base_url.startswith("https://")

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        log.info("engine=%s run_engine=%s dev_auth=%s service_mode=%s", s.omr_engine, s.run_engine, s.dev_auth, s.service_mode)
        yield
        jobs.stop()

    app = FastAPI(title="spm-note-annotator", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store, app.state.jobs = s, store, jobs

    @app.exception_handler(ApiError)
    async def _api_err(request, exc: ApiError):
        return JSONResponse(exc.body, status_code=exc.status)

    # ---------- session helpers ----------
    def set_session(resp: Response, uid: int):
        resp.set_cookie(COOKIE, ser.dumps({"uid": uid}), max_age=SESSION_TTL, httponly=True,
                        samesite="lax", secure=secure_cookie, path="/")

    SERVICE_QUOTA = 10**9  # 服務模式：額度由上游管，這裡不設限（reserve 仍負責同檔案同日去重）

    def service_user(request: Request) -> dict:
        given = request.headers.get("x-spm-service-key", "")
        if not hmac.compare_digest(given.encode("utf-8"), s.service_key.encode("utf-8")):
            raise ApiError(401, "unauthorized")
        uid = request.headers.get("x-spm-user-id", "")
        if not uid.isdigit() or not (0 < int(uid) < 2**53):
            raise ApiError(400, "bad_user")
        name = unquote(request.headers.get("x-spm-user-name", ""))[:50] or f"user{uid}"
        store.upsert_user(int(uid), name, SERVICE_QUOTA, dev=False)
        return {"id": int(uid), "name": name, "quota_limit": SERVICE_QUOTA}

    def current_user(request: Request) -> dict:
        if s.service_mode:
            return service_user(request)
        raw = request.cookies.get(COOKIE)
        if raw:
            try:
                uid = ser.loads(raw, max_age=SESSION_TTL).get("uid")
                u = store.get_user(int(uid))
                if u:
                    return u
            except (BadSignature, ValueError, TypeError, AttributeError):
                pass
        raise ApiError(401, "unauthenticated")

    def safe_next(n: str | None) -> str:
        if not n or not n.startswith("/") or n.startswith("//") or "\\" in n or "\n" in n or "\r" in n:
            return "/"
        return n

    if not s.service_mode:  # 服務模式：登入／session／額度報表由上游負責，這裡不開放
        # ---------- auth ----------
        @app.get("/auth/login")
        def auth_login(next: str = "/"):
            nxt = safe_next(next)
            if not s.wp_base_url:
                if s.dev_auth:
                    return RedirectResponse(f"/auth/dev-login?next={quote(nxt, safe='')}", 302)
                raise ApiError(503, "wp_not_configured")
            ret = f"{s.app_base_url}/auth/callback?next={quote(nxt, safe='')}"
            return RedirectResponse(f"{s.wp_base_url}/?spm_tool_auth=1&return={quote(ret, safe='')}", 302)

        @app.get("/auth/callback")
        def auth_callback(token: str = "", next: str = "/"):
            if not s.auth_secret:
                raise ApiError(503, "auth_not_configured")
            try:
                c = tokens.verify(s.auth_secret, token)
            except tokens.TokenError as e:
                raise ApiError(400, "invalid_token", reason=e.reason)
            if not store.use_nonce(c["nonce"], c["exp"]):
                raise ApiError(400, "invalid_token", reason="replayed")
            store.upsert_user(c["sub"], c["name"], c["quota"], dev=False)
            resp = RedirectResponse(safe_next(next), 303)
            set_session(resp, c["sub"])
            return resp

        @app.post("/auth/logout")
        def auth_logout():
            resp = JSONResponse({"ok": True})
            resp.delete_cookie(COOKIE, path="/")
            return resp

        if s.dev_auth:
            @app.get("/auth/dev-login")
            def dev_login(name: str = "測試者", quota: int | None = None, next: str = "/"):
                q = s.dev_daily_quota if quota is None else max(0, min(1000, quota))
                name = name[:50] or "測試者"
                uid = 9_000_000 + int(hashlib.sha1(name.encode("utf-8")).hexdigest()[:6], 16) % 1_000_000
                store.upsert_user(uid, name, q, dev=True)
                resp = RedirectResponse(safe_next(next), 303)
                set_session(resp, uid)
                return resp

    # ---------- api ----------
    @app.get("/api/me")
    def api_me(request: Request):
        u = current_user(request)
        return {"user": {"id": u["id"], "name": u["name"]}, "quota": None if s.service_mode else store.quota_info(u["id"]), "dev": s.dev_auth,
                "limits": {"max_upload_mb": s.max_upload_mb, "max_pages": s.max_pages,
                           "retention_hours": s.retention_hours, "est_seconds_per_page": s.est_seconds_per_page},
                **({"remote": jobs.remote_status()} if s.service_mode else {})}

    @app.post("/api/omr", status_code=202)
    def api_omr(request: Request, file: UploadFile = File(...), engine: str | None = Form(None)):
        u = current_user(request)
        eng = (engine or s.omr_engine).strip()
        if eng not in s.engine_names():
            raise ApiError(400, "unknown_engine")
        limit = s.max_upload_mb * 1024 * 1024
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit + 1_000_000:
            raise ApiError(413, "file_too_large")
        job_id = uuid.uuid4().hex
        d = jobs.dir(job_id)
        d.mkdir(parents=True)
        tmp = d / "upload.tmp"
        try:
            h = hashlib.sha256()
            size = 0
            with open(tmp, "wb") as out:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ApiError(413, "file_too_large")
                    h.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise ApiError(422, "empty_file")
            try:
                kind, pages = pipeline.validate_upload(tmp, file.filename or "", s)
            except pipeline.UploadError as e:
                raise ApiError(e.status, e.code, **({"detail": e.detail} if e.detail else {}))
            with open(tmp, "rb") as f:
                sniffed = pipeline.sniff(f.read(2048))
            src_name = "source." + {"pdf": "pdf", "png": "png", "jpg": "jpg", "webp": "webp"}[sniffed]
            tmp.replace(d / src_name)
            sha = h.hexdigest()
            decision, jid = store.reserve(u["id"], sha, job_id)
            if decision == "exceeded":
                raise ApiError(429, "quota_exceeded", quota=store.quota_info(u["id"]))
            if decision == "cached":
                shutil.rmtree(d, ignore_errors=True)
                return {"job_id": jid, "cached": True}
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        import json as _json
        (d / "meta.json").write_text(_json.dumps({"source_file": src_name, "kind": kind, "sha256": sha}),
                                     encoding="utf-8", newline="\n")
        store.create_job(job_id, u["id"], eng, kind, (file.filename or "upload")[:200], pages)
        jobs.submit(job_id)
        return {"job_id": job_id, "cached": False}

    def own_job(request: Request, job_id: str) -> dict:
        u = current_user(request)
        j = store.get_job(job_id) if JOB_ID_RE.match(job_id) else None
        if not j or j["user_id"] != u["id"]:
            raise ApiError(404, "not_found")
        return j

    @app.get("/api/omr/{job_id}")
    def api_job(request: Request, job_id: str):
        j = own_job(request, job_id)
        body = {"status": j["status"], "progress": jobs.progress(j),
                "error": j["error"], "result": None}
        if j["status"] == "done":
            body["result"] = jobs.result(job_id)
        return body

    @app.get("/api/omr/{job_id}/page/{i}.png")
    def api_page(request: Request, job_id: str, i: int):
        j = own_job(request, job_id)
        p = jobs.dir(job_id) / f"page{i}.png"
        if i < 0 or i >= j["pages"] or not p.exists():
            raise ApiError(404, "not_found")
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/export")
    def api_export(request: Request, req: ExportReq):
        j = own_job(request, req.job_id)
        if j["status"] != "done":
            raise ApiError(409, "job_not_done")
        fmt = req.format.lower()
        if fmt not in ("pdf", "png"):
            raise ApiError(422, "bad_format")
        result, meta = jobs.result(req.job_id), jobs.meta(req.job_id)
        color = exp.parse_color(req.style.color)
        labels = [lb.model_dump() for lb in req.labels]
        d = jobs.dir(req.job_id)
        stem = re.sub(r"[^\w\-]+", "_", (j["filename"] or "score").rsplit(".", 1)[0])[:60] or "score"
        try:
            if meta["kind"] == "pdf":
                if fmt == "png" and req.page is None:
                    raise ApiError(422, "page_required")
                data, mt = exp.export_pdf_source(d / meta["source_file"], result, labels, req.style.font_size_px,
                                                 color, fmt, req.page, int(meta.get("dpi", 300)))
            else:
                data, mt = exp.export_image_source(d / "page0.png", result, labels, req.style.font_size_px, color, fmt)
        except exp.ExportError as e:
            raise ApiError(e.status, e.code)
        ext = "pdf" if mt == "application/pdf" else "png"
        suffix = f"_p{req.page + 1}" if (fmt == "png" and req.page is not None) else ""
        return Response(data, media_type=mt, headers={
            "Content-Disposition": f"attachment; filename=\"annotated{suffix}.{ext}\"; filename*=UTF-8''{quote(stem)}_annotated{suffix}.{ext}",
            "Cache-Control": "no-store"})

    if not s.service_mode:  # 服務模式：登入／session／額度報表由上游負責，這裡不開放
        # ---------- admin ----------
        @app.get("/admin/usage")
        def admin_usage(request: Request, token: str = ""):
            given = request.headers.get("x-admin-token") or token
            if not s.admin_token or not hmac.compare_digest(given.encode(), s.admin_token.encode()):
                raise ApiError(403, "forbidden")
            return {"date": clock.today_taipei(), "users": store.usage_report()}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---------- static frontend（服務模式不提供；前端由上游提供）----------
    if s.service_mode:
        pass
    elif s.web_dir and (s.web_dir / "index.html").exists():
        app.mount("/", NoCacheStatic(directory=str(s.web_dir), html=True), name="web")
    else:
        @app.get("/")
        def placeholder():
            return Response("web/ frontend not found", media_type="text/plain; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return create_app()
