"""Environment-driven settings. Secret values are never logged."""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("spm.config")

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = SERVER_DIR.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def load_engines(root: Path) -> dict:
    p = root / "omr" / "engines.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class Settings:
    app_base_url: str = "http://localhost:8787"
    wp_base_url: str = ""
    auth_secret: str = ""
    dev_auth: bool = False
    dev_daily_quota: int = 5
    max_upload_mb: int = 30
    max_pages: int = 10
    omr_engine: str = ""
    timeout_per_page_s: int = 600
    data_dir: Path = SERVER_DIR / "data"
    retention_hours: int = 24
    est_seconds_per_page: int = 60  # 前端顯示的辨識預估時間（每頁秒數）；僅提示用
    session_secret: str = ""
    admin_token: str = ""
    service_key: str = ""  # 非空 = 服務模式：只信任帶正確 X-SPM-Service-Key 的請求（由上游網站代理），額度由上游管
    concurrency: int = 1
    worker_mode: bool = False  # WORKER_MODE=1: Cloud Run 單頁辨識 worker（只開 /healthz 與 /api/page）
    worker_key: str = ""  # X-SPM-Worker-Key 的值（worker 模式必填；dispatcher 也用同一把送出）
    remote_worker_url: str = ""  # 非空 = dispatcher：整份工作的頁面平行送到這個 worker，失敗再退回本機
    remote_max_parallel: int = 20
    remote_timeout_s: int = 240
    worker_max_image_mb: int = 25
    # 每月免費額度保護：dispatcher 自己量測 worker 請求牆鐘秒數 x 規格，用量達 比例 x 免費額度 就改本機辨識
    remote_vcpu: float = 2.0
    remote_mem_gib: float = 2.0
    remote_free_vcpu_seconds: float = 180000.0
    remote_free_gib_seconds: float = 360000.0
    remote_cap_ratio: float = 0.8
    remote_est_page_seconds: float = 60.0
    root: Path = ROOT_DIR
    run_engine: Path | None = None
    web_dir: Path | None = None
    engines: dict = field(default_factory=dict)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.app_base_url = self.app_base_url.rstrip("/")
        self.wp_base_url = self.wp_base_url.rstrip("/")
        if not self.engines:
            self.engines = load_engines(self.root)
        if self.run_engine is None:
            cand = self.root / "omr" / "run_engine.py"
            self.run_engine = cand if cand.exists() else SERVER_DIR / "app" / "mock_engine.py"
        if self.web_dir is None:
            self.web_dir = self.root / "web"
        if not self.omr_engine:
            d = self.engines.get("default")
            self.omr_engine = d if isinstance(d, str) else "mock"
        if self.service_key:
            if len(self.service_key) < 16:
                raise ValueError("SERVICE_KEY must be at least 16 characters")
            if self.dev_auth:
                log.warning("SERVICE_KEY set: service mode wins, DEV_AUTH is ignored.")
                self.dev_auth = False
        self.remote_worker_url = self.remote_worker_url.strip().rstrip("/")
        self.remote_max_parallel = max(1, self.remote_max_parallel)
        if self.worker_mode or self.remote_worker_url:
            if len(self.worker_key) < 32:
                raise ValueError("WORKER_KEY must be at least 32 characters (worker mode / REMOTE_WORKER_URL)")
        if self.worker_mode and self.remote_worker_url:
            raise ValueError("WORKER_MODE and REMOTE_WORKER_URL are mutually exclusive")
        if not self.session_secret:
            self.session_secret = secrets.token_urlsafe(32)
            log.warning("SESSION_SECRET not set: generated a random one (sessions reset on restart).")
        if not self.auth_secret and self.dev_auth:
            self.auth_secret = secrets.token_urlsafe(32)
            log.warning("SPM_TOOLS_AUTH_SECRET not set: generated a random dev secret (upstream handoff will not work).")

    @property
    def remote_enabled(self) -> bool:
        return bool(self.remote_worker_url) and not self.worker_mode

    @property
    def service_mode(self) -> bool:
        return bool(self.service_key)

    # -- engines --
    def engine_names(self) -> list[str]:
        names = {k for k, v in self.engines.items() if isinstance(v, dict)}
        names.add("mock")
        return sorted(names)

    def engine_python(self, engine: str) -> str:
        import sys

        v = self.engines.get(engine)
        if isinstance(v, dict) and v.get("python") and Path(v["python"]).exists():
            return v["python"]
        return sys.executable

    def engine_dpi(self, engine: str) -> int:
        v = self.engines.get(engine)
        if isinstance(v, dict):
            try:
                return max(72, min(600, int(v.get("dpi", 300))))
            except (TypeError, ValueError):
                pass
        return 300

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        remote_url = e.get("REMOTE_WORKER_URL", "").strip()
        kw = dict(
            app_base_url=e.get("APP_BASE_URL", "http://localhost:8787"),
            wp_base_url=e.get("WP_BASE_URL", ""),
            auth_secret=e.get("SPM_TOOLS_AUTH_SECRET", ""),
            dev_auth=e.get("DEV_AUTH", "") == "1",
            dev_daily_quota=_int("DEV_DAILY_QUOTA", 5),
            max_upload_mb=_int("MAX_UPLOAD_MB", 30),
            max_pages=_int("MAX_PAGES", 10),
            omr_engine=e.get("OMR_ENGINE", ""),
            timeout_per_page_s=_int("OMR_TIMEOUT_PER_PAGE_S", 600),
            retention_hours=_int("JOB_RETENTION_HOURS", 24),
            est_seconds_per_page=_int("OMR_EST_SECONDS_PER_PAGE", 60),
            session_secret=e.get("SESSION_SECRET", ""),
            admin_token=e.get("ADMIN_TOKEN", ""),
            service_key=e.get("SERVICE_KEY", "").strip(),
            # 有遠端 worker 時工作不必排隊等同一顆 CPU：預設放寬到 8（本機退回仍由 JobManager 的號誌限 1）
            concurrency=max(1, _int("OMR_CONCURRENCY", 8 if remote_url else 1)),
            worker_mode=e.get("WORKER_MODE", "") == "1",
            worker_key=e.get("WORKER_KEY", "").strip(),
            remote_worker_url=remote_url,
            remote_max_parallel=_int("REMOTE_MAX_PARALLEL", 20),
            remote_timeout_s=_int("REMOTE_TIMEOUT_S", 240),
            worker_max_image_mb=_int("WORKER_MAX_IMAGE_MB", 25),
            remote_vcpu=_float("REMOTE_VCPU", 2.0),
            remote_mem_gib=_float("REMOTE_MEM_GIB", 2.0),
            remote_free_vcpu_seconds=_float("REMOTE_FREE_VCPU_SECONDS", 180000.0),
            remote_free_gib_seconds=_float("REMOTE_FREE_GIB_SECONDS", 360000.0),
            remote_cap_ratio=_float("REMOTE_CAP_RATIO", 0.8),
            remote_est_page_seconds=_float("REMOTE_EST_PAGE_SECONDS", 60.0),
        )
        if e.get("DATA_DIR"):
            kw["data_dir"] = Path(e["DATA_DIR"])
        return cls(**kw)
