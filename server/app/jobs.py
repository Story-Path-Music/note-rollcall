"""Background OMR job queue."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from pathlib import Path

from . import clock, pipeline, progress as prog, remote
from .config import Settings
from .store import Store

log = logging.getLogger("spm.jobs")


class JobManager:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.jobs_dir = settings.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=settings.concurrency, thread_name_prefix="omr")
        # 本機辨識的並行上限：沒有遠端 worker = OMR_CONCURRENCY（行為不變）；有遠端 = 1（退回的頁面不能壓垮本機）
        self.local_sem = threading.Semaphore(1 if settings.remote_enabled else settings.concurrency)
        self.remote_pool = (ThreadPoolExecutor(max_workers=settings.remote_max_parallel, thread_name_prefix="remote")
                            if settings.remote_enabled else None)
        self.capped_gate = threading.Semaphore(1)  # 額度用盡後新工作一次只跑 1 份（等同本機行為）
        self._cap_logged: str | None = None
        self._stop = threading.Event()
        self._trackers: dict[str, prog.JobProgress] = {}
        self._tlock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- lifecycle ----
    def start(self):
        for jid in self.store.interrupted_jobs():  # server restarted mid-job
            self._fail(jid, "interrupted")
        self.cleanup()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="job-cleanup")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.remote_pool:
            self.remote_pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        while not self._stop.wait(3600):
            try:
                self.cleanup()
            except Exception:
                log.exception("cleanup failed")

    def cleanup(self):
        cutoff = clock.now() - timedelta(hours=self.s.retention_hours)
        n = 0
        for jid in self.store.old_jobs(cutoff.isoformat()):
            shutil.rmtree(self.jobs_dir / jid, ignore_errors=True)
            self.store.delete_job(jid)
            with self._tlock:
                self._trackers.pop(jid, None)
            n += 1
        # orphan dirs (no db row) older than cutoff
        for d in self.jobs_dir.iterdir():
            try:
                if d.is_dir() and not self.store.get_job(d.name) and d.stat().st_mtime < cutoff.timestamp():
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                pass
        if n:
            log.info("cleaned %d expired jobs", n)

    # ---- paths ----
    def dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def meta(self, job_id: str) -> dict:
        return json.loads((self.dir(job_id) / "meta.json").read_text(encoding="utf-8"))

    def result(self, job_id: str) -> dict | None:
        p = self.dir(job_id) / "result.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    # ---- progress ----
    def progress(self, job: dict) -> dict:
        """The `progress` object of GET /api/omr/{id} (CONTRACT section 2)."""
        pages, est = job["pages"], self.s.est_seconds_per_page
        capped = self.remote_capped()
        c = 1 if capped else self.s.concurrency
        # 遠端模式：整份工作的頁面平行辨識，牆鐘時間約 ceil(頁數/平行度) 頁（額度用盡 = 回到本機序列）
        own_pages = -(-pages // self.s.remote_max_parallel) if (self.s.remote_enabled and not capped) else pages
        out = {"page": job["page"], "pages": pages, "stage": "queued", "fraction": 0.0,
               "queue_position": 0, "eta_seconds": None}
        st = job["status"]
        if st == "done":
            out.update(page=pages, stage="done", fraction=1.0, eta_seconds=0)
            return out
        if st == "queued":
            ahead_pages = self.store.pages_ahead(job["job_id"])
            ahead = self.store.queue_position(job["job_id"])
            # 同時可跑 c 個工作：前面的工作少於 c 個就不必排隊（c=1 時與原本相同）
            out["queue_position"] = max(0, ahead - c + 1) if ahead else 0
            out["eta_seconds"] = int(est * (own_pages + ahead_pages / c)) if est > 0 else None
            return out
        with self._tlock:
            tr = self._trackers.get(job["job_id"])
        if tr is None:  # error (tracker dropped) or a status race
            return out
        page, stage, frac, elapsed = tr.snapshot()
        out.update(page=page, stage=stage, fraction=round(frac, 4))
        if st == "running":
            out["eta_seconds"] = prog.eta_seconds(frac, elapsed, own_pages, est)
        return out

    # ---- execution ----
    def submit(self, job_id: str):
        self.pool.submit(self._run, job_id)

    def _fail(self, job_id: str, err: str):
        self.store.update_job(job_id, status="error", error=err)
        self.store.finish_usage(job_id, False)  # refund

    # ---- Cloud Run monthly cap ----
    def _caps(self) -> tuple[float, float]:
        s = self.s
        return s.remote_free_vcpu_seconds * s.remote_cap_ratio, s.remote_free_gib_seconds * s.remote_cap_ratio

    def _est(self) -> tuple[float, float]:
        s = self.s
        return s.remote_est_page_seconds * s.remote_vcpu, s.remote_est_page_seconds * s.remote_mem_gib

    def remote_capped(self) -> bool:
        """True when one more estimated page would not fit under ratio x free tier this billing month."""
        if not self.s.remote_enabled:
            return False
        v, g = self.store.remote_usage(clock.billing_month())
        (cv, cg), (ev, eg) = self._caps(), self._est()
        return v + ev > cv or g + eg > cg

    def remote_status(self) -> dict | None:
        if not self.s.remote_enabled:
            return None
        m = clock.billing_month()
        v, g = self.store.remote_usage(m)
        return {"month": m, "vcpu_seconds": round(v, 1), "gib_seconds": round(g, 1),
                "cap_ratio": self.s.remote_cap_ratio, "capped": self.remote_capped()}

    def _reserve_remote(self):
        """Reserve the estimated cost of one page before sending.  Returns (month, est_v, est_g) or None (capped)."""
        m = clock.billing_month()
        ev, eg = self._est()
        if self.store.remote_add(m, ev, eg, cap=self._caps()):
            return m, ev, eg
        if self._cap_logged != m:
            self._cap_logged = m
            log.warning("remote cap reached for %s: Cloud Run disabled, local recognition until next month", m)
        return None

    def _run(self, job_id: str):
        try:
            if self.remote_capped():  # 額度用盡：新工作一次一份，且仍維持 queued 直到輪到
                with self.capped_gate:
                    self._process(job_id)
            else:
                self._process(job_id)
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, RuntimeError) else "internal_error"
            if not isinstance(e, RuntimeError):
                log.exception("job %s crashed", job_id)
            self._fail(job_id, msg[:300])

    def _process(self, job_id: str):
        job = self.store.get_job(job_id)
        if not job:
            return
        d = self.dir(job_id)
        meta = self.meta(job_id)
        engine = job["engine"]
        dpi = self.s.engine_dpi(engine)
        meta["dpi"] = dpi
        tr = prog.JobProgress(job["pages"])
        with self._tlock:
            self._trackers[job_id] = tr
        self.store.update_job(job_id, status="running", page=1)
        src = d / meta["source_file"]
        if self.s.remote_enabled:
            pages_out = self._process_remote(job_id, job, d, meta, src, engine, dpi, tr)
        else:
            pages_out = []
            for i in range(job["pages"]):
                self.store.update_job(job_id, page=i + 1)
                tr.begin_page(i + 1)
                png, pg, size = self._render(meta, src, d, i, dpi)
                tr.mark("render", 1.0)
                pages_out.append(self._local_page(job_id, engine, i, png, pg, size, d, tr.mark))
        result = {
            "source": {"type": meta["kind"], "filename": job["filename"], "page_count": job["pages"]},
            "engine": engine, "pages": pages_out,
            "warnings": [w for p in pages_out for w in p["warnings"]],
        }
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
        (d / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8", newline="\n")
        self.store.update_job(job_id, status="done")
        self.store.finish_usage(job_id, True)
        with self._tlock:
            self._trackers.pop(job_id, None)

    # ---- page helpers ----
    def _render(self, meta, src: Path, d: Path, i: int, dpi: int):
        png = d / f"page{i}.png"
        if meta["kind"] == "pdf":
            pg = pipeline.rasterize_page(src, i, png, dpi)
            return png, pg, (pg["w"], pg["h"])
        return png, None, pipeline.normalize_image(src, png)

    def _local_page(self, job_id, engine, i, png, pg, size, d: Path, on_progress=None) -> dict:
        with self.local_sem:
            raw = pipeline.run_engine(self.s, engine, png, d / f"page{i}.json", on_progress=on_progress)
        page = pipeline.merge_page(i, raw, pg, size)
        page["image_url"] = f"/api/omr/{job_id}/page/{i}.png"
        return page

    def _remote_page(self, png: Path, engine: str, i: int, pg) -> dict | None:
        """One worker call, one retry on network error / 5xx / timeout.  None = give up (caller falls back local)."""
        for attempt in (1, 2):
            res = self._reserve_remote()
            if res is None:
                return None  # cap reached: caller recognises locally
            m, ev, eg = res
            t0 = time.monotonic()
            try:
                return remote.recognize_page(self.s, png, engine, i, pg)
            except remote.RemoteError as e:
                if e.fatal:
                    raise RuntimeError(str(e))
                log.warning("remote page %d attempt %d failed: %s", i, attempt, e)
                if not e.retryable:
                    break
            finally:  # settle: measured wall clock of the request (includes cold start) replaces the estimate
                el = time.monotonic() - t0
                self.store.remote_add(m, el * self.s.remote_vcpu - ev, el * self.s.remote_mem_gib - eg)
        return None

    def _process_remote(self, job_id, job, d: Path, meta, src: Path, engine: str, dpi: int, tr) -> list:
        n = job["pages"]
        results: dict[int, dict] = {}
        info: dict[int, tuple] = {}
        futs = {}
        done = 0

        def collect(finished):
            nonlocal done
            for f in finished:
                i = futs.pop(f)
                page = f.result()  # RuntimeError (fatal engine failure) propagates -> job error
                png, pg, size = info[i]
                if page is None:
                    log.info("job %s page %d: local fallback", job_id, i)
                    page = self._local_page(job_id, engine, i, png, pg, size, d)
                page["image_url"] = f"/api/omr/{job_id}/page/{i}.png"
                results[i] = page
                done += 1
                self.store.update_job(job_id, page=min(n, done + 1))
                tr.remote_progress(len(info), done)

        try:
            for i in range(n):  # render page i while earlier pages are already being recognised remotely
                png, pg, size = self._render(meta, src, d, i, dpi)
                info[i] = (png, pg, size)
                tr.remote_progress(len(info), done)
                futs[self.remote_pool.submit(self._remote_page, png, engine, i, pg)] = i
                collect({f for f in futs if f.done()})
            while futs:
                fin, _ = wait(futs, return_when=FIRST_COMPLETED)
                collect(fin)
        except BaseException:
            for f in futs:
                f.cancel()
            raise
        return [results[i] for i in range(n)]
