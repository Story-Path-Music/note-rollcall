"""Cloud Run burst: page worker endpoint + dispatcher (docs/CONTRACT.md section 5)."""
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import pipeline, remote
from app.main import create_app
from conftest import make_pdf, make_png

WKEY = "w" * 40
SKEY = "s" * 24
HDR = {"X-SPM-Service-Key": SKEY, "X-SPM-User-Id": "7"}


def worker_settings(tmp_settings, **kw):
    return tmp_settings(**{"worker_mode": True, "worker_key": WKEY, "dev_auth": False, **kw})


def png_upload(png=None):
    return {"file": ("page.png", png or make_png(), "image/png")}


# ---------- worker ----------
def test_worker_only_exposes_page_and_health(tmp_settings):
    with TestClient(create_app(worker_settings(tmp_settings))) as c:
        assert c.get("/healthz").json()["ok"] is True
        assert c.get("/api/me").status_code == 404
        assert c.post("/api/omr", files=png_upload()).status_code == 404
        assert c.get("/").status_code == 404


def test_worker_key_required_and_min_length(tmp_settings):
    with pytest.raises(ValueError):
        worker_settings(tmp_settings, worker_key="short")
    with pytest.raises(ValueError):
        tmp_settings(worker_mode=True, worker_key="")


def test_worker_auth(tmp_settings):
    with TestClient(create_app(worker_settings(tmp_settings))) as c:
        assert c.post("/api/page", files=png_upload()).status_code == 401
        assert c.post("/api/page", files=png_upload(), headers={"X-SPM-Worker-Key": "x" * 40}).status_code == 401
        assert c.post("/api/page", files=png_upload(), headers={"X-SPM-Worker-Key": WKEY[:-1]}).status_code == 401


def test_worker_single_page_ok(tmp_settings):
    with TestClient(create_app(worker_settings(tmp_settings))) as c:
        r = c.post("/api/page", files=png_upload(make_png(800, 600)), headers={"X-SPM-Worker-Key": WKEY},
                   data={"index": "3", "pt_w": "595.3", "pt_h": "841.9"})
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["index"] == 3 and p["width_px"] == 800 and p["height_px"] == 600
        assert p["pdf_width_pt"] == 595.3 and len(p["notes"]) == 8
        assert all(n["id"].startswith("p3") for n in p["notes"])


def test_worker_rejects_bad_input(tmp_settings):
    h = {"X-SPM-Worker-Key": WKEY}
    with TestClient(create_app(worker_settings(tmp_settings, worker_max_image_mb=1))) as c:
        assert c.post("/api/page", files={"file": ("a.pdf", make_pdf(1), "application/pdf")}, headers=h).status_code == 415
        assert c.post("/api/page", files={"file": ("a.png", b"\x89PNG\r\n\x1a\nxxxx", "image/png")}, headers=h).status_code == 422
        assert c.post("/api/page", files={"file": ("a.png", b"", "image/png")}, headers=h).status_code == 422
        big = make_png(2000, 2000)
        assert c.post("/api/page", files={"file": ("a.png", big + b"\0" * 1_200_000, "image/png")}, headers=h).status_code == 413
        assert c.post("/api/page", files=png_upload(), headers=h, data={"engine": "nope"}).status_code == 400


def test_worker_engine_failure_is_422(tmp_settings, monkeypatch):
    monkeypatch.setenv("MOCK_ENGINE_FAIL", "1")
    with TestClient(create_app(worker_settings(tmp_settings))) as c:
        r = c.post("/api/page", files=png_upload(), headers={"X-SPM-Worker-Key": WKEY})
        assert r.status_code == 422 and r.json()["error"].startswith("engine_failed")


# ---------- dispatcher ----------
def dispatcher_settings(tmp_settings, **kw):
    base = dict(service_key=SKEY, dev_auth=False, remote_worker_url="http://worker.invalid", worker_key=WKEY,
                concurrency=4)
    base.update(kw)
    return tmp_settings(**base)


class FakeRemote:
    """Replaces remote.recognize_page; records calls, optional per-page delay/failure."""

    def __init__(self):
        self.calls, self.active, self.max_active = [], 0, 0
        self.lock = threading.Lock()
        self.delay = {}
        self.fail = {}  # index -> list of exceptions to raise first

    def __call__(self, s, png, engine, index, pg):
        with self.lock:
            self.calls.append(index)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            err = self.fail.get(index, [])
            e = err.pop(0) if err else None
        try:
            time.sleep(self.delay.get(index, 0.05))
            if e:
                raise e
            return {"index": index, "image_url": None, "width_px": 10, "height_px": 10, "pdf_width_pt": None,
                    "pdf_height_pt": None, "staves": [], "notes": [{"id": f"p{index}n0", "step": "C"}],
                    "elapsed_ms": 1, "warnings": [f"remote{index}"]}
        finally:
            with self.lock:
                self.active -= 1


def submit(c, pages, user="7"):
    h = {**HDR, "X-SPM-User-Id": user}
    r = c.post("/api/omr", headers=h, files={"file": ("x.pdf", make_pdf(pages, tag=str(time.time_ns())), "application/pdf")})
    assert r.status_code == 202, r.text
    return h, r.json()["job_id"]


def poll(c, hj, timeout=30):
    h, jid = hj
    seen, t0 = [], time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/omr/{jid}", headers=h).json()
        seen.append(r["progress"])
        if r["status"] in ("done", "error"):
            return r, seen
        time.sleep(0.02)
    raise AssertionError("timeout")


def test_dispatcher_parallel_and_page_order(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.delay = {0: 0.5, 1: 0.3, 2: 0.1, 3: 0.2}  # complete out of order
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        t0 = time.time()
        r, seen = poll(c, submit(c, 4))
        took = time.time() - t0
        assert r["status"] == "done", r
        pages = r["result"]["pages"]
        assert [p["index"] for p in pages] == [0, 1, 2, 3]
        assert [p["notes"][0]["id"] for p in pages] == ["p0n0", "p1n0", "p2n0", "p3n0"]
        assert all(p["image_url"].endswith(f"/page/{i}.png") for i, p in enumerate(pages))
        assert fr.max_active >= 3 and took < 2.5  # parallel (max_active is the real check)
        fracs = [p["fraction"] for p in seen]
        assert fracs == sorted(fracs)
        assert all(f < 1.0 for f in fracs[:-1]) and seen[-1]["stage"] == "done" and seen[-1]["fraction"] == 1.0
        assert all(0 <= p["page"] <= p["pages"] for p in seen)


def test_dispatcher_progress_monotonic_and_stages(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.delay = {i: 0.15 * (i + 1) for i in range(5)}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        r, seen = poll(c, submit(c, 5))
        fracs = [p["fraction"] for p in seen]
        assert fracs == sorted(fracs)
        running = [p for p in seen if p["stage"] not in ("queued", "done")]
        assert running and all(p["fraction"] <= 0.99 for p in running)
        assert {p["stage"] for p in running} <= {"render", "detect", "transcribe", "post"}
        assert len({p["fraction"] for p in running}) >= 3  # advances as pages complete


def test_retry_once_then_success(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.fail = {1: [remote.RemoteError("boom", retryable=True)]}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        r, _ = poll(c, submit(c, 2))
        assert r["status"] == "done"
        assert fr.calls.count(1) == 2 and fr.calls.count(0) == 1
        assert r["result"]["pages"][1]["warnings"] == ["remote1"]


def test_retry_then_local_fallback(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.fail = {1: [remote.RemoteError("down", retryable=True)] * 2}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        r, _ = poll(c, submit(c, 3))
        assert r["status"] == "done", r
        assert fr.calls.count(1) == 2  # one retry, then local
        pg = r["result"]["pages"]
        assert [p["index"] for p in pg] == [0, 1, 2]
        assert pg[0]["warnings"] == ["remote0"]
        assert pg[1]["warnings"] == [] and len(pg[1]["notes"]) == 8  # mock engine ran locally
        assert pg[1]["notes"][0]["id"].startswith("p1")


def test_non_retryable_4xx_goes_straight_to_local(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.fail = {0: [remote.RemoteError("worker http 401", retryable=False)]}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        r, _ = poll(c, submit(c, 1))
        assert r["status"] == "done" and fr.calls == [0]


def test_fatal_engine_failure_fails_job(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.fail = {0: [remote.RemoteError("engine_failed: no noteheads", fatal=True)]}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        r, _ = poll(c, submit(c, 2))
        assert r["status"] == "error" and r["error"].startswith("engine_failed")


def test_unset_remote_url_is_local_path(tmp_settings, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("remote must not be used")
    monkeypatch.setattr(remote, "recognize_page", boom)
    s = tmp_settings(service_key=SKEY, dev_auth=False)
    assert not s.remote_enabled
    with TestClient(create_app(s)) as c:
        r, _ = poll(c, submit(c, 2))
        assert r["status"] == "done" and len(r["result"]["pages"]) == 2


def test_remote_parallel_cap_is_global(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.delay = {i: 0.2 for i in range(6)}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings, remote_max_parallel=2))) as c:
        r, _ = poll(c, submit(c, 6))
        assert r["status"] == "done" and fr.max_active == 2


def test_local_fallback_limited_to_one_at_a_time(tmp_settings, monkeypatch):
    """Three concurrent jobs, every remote call fails: local engine runs must never overlap."""
    active = {"n": 0, "max": 0}
    lock = threading.Lock()
    real = pipeline.run_engine

    def counting(*a, **k):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        try:
            time.sleep(0.1)
            return real(*a, **k)
        finally:
            with lock:
                active["n"] -= 1
    monkeypatch.setattr(pipeline, "run_engine", counting)

    def always_fail(*a, **k):
        raise remote.RemoteError("down", retryable=True)
    monkeypatch.setattr(remote, "recognize_page", always_fail)
    with TestClient(create_app(dispatcher_settings(tmp_settings))) as c:
        jobs = [submit(c, 2, user=str(u)) for u in (1, 2, 3)]
        for hj in jobs:
            r, _ = poll(c, hj)
            assert r["status"] == "done"
        assert active["max"] == 1


def test_jobs_run_concurrently_with_remote(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.delay = {0: 1.5}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings, concurrency=3))) as c:
        jobs = [submit(c, 1, user=str(u)) for u in (1, 2, 3)]
        time.sleep(0.25)
        sts = [c.get(f"/api/omr/{j}", headers=h).json() for h, j in jobs]
        assert [x["status"] for x in sts] == ["running"] * 3
        assert all(x["progress"]["queue_position"] == 0 for x in sts)


def test_queue_position_respects_concurrency(tmp_settings, monkeypatch):
    fr = FakeRemote()
    fr.delay = {0: 2.0}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(dispatcher_settings(tmp_settings, concurrency=2))) as c:
        jobs = [submit(c, 1, user=str(u)) for u in (1, 2, 3, 4)]
        time.sleep(0.25)
        pos = [c.get(f"/api/omr/{j}", headers=h).json()["progress"]["queue_position"] for h, j in jobs]
        assert pos == [0, 0, 1, 2]


def test_default_concurrency_when_remote_configured(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("OMR_CONCURRENCY", raising=False)
    monkeypatch.setenv("REMOTE_WORKER_URL", "https://w.example/")
    monkeypatch.setenv("WORKER_KEY", WKEY)
    s = Settings.from_env()
    assert s.concurrency == 8 and s.remote_max_parallel == 20 and s.remote_timeout_s == 240
    assert s.remote_worker_url == "https://w.example"
    monkeypatch.delenv("REMOTE_WORKER_URL")
    monkeypatch.delenv("WORKER_KEY")
    assert Settings.from_env().concurrency == 1


# ---------- real HTTP: worker app <-> remote.recognize_page ----------
@pytest.fixture
def live_worker(tmp_settings):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(worker_settings(tmp_settings)), host="127.0.0.1", port=port,
                                        log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def test_client_against_live_worker(tmp_settings, live_worker, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(make_png(500, 400))
    s = dispatcher_settings(tmp_settings, remote_worker_url=live_worker)
    p = remote.recognize_page(s, png, "mock", 2, {"pt_w": 595.0, "pt_h": 842.0})
    assert p["index"] == 2 and p["width_px"] == 500 and p["pdf_width_pt"] == 595.0
    bad = dispatcher_settings(tmp_settings, remote_worker_url=live_worker, worker_key="z" * 40)
    with pytest.raises(remote.RemoteError) as ei:
        remote.recognize_page(bad, png, "mock", 0, None)
    assert not ei.value.retryable and "401" in str(ei.value)
    dead = dispatcher_settings(tmp_settings, remote_worker_url="http://127.0.0.1:9", remote_timeout_s=2)
    with pytest.raises(remote.RemoteError) as ei:
        remote.recognize_page(dead, png, "mock", 0, None)
    assert ei.value.retryable


# ---------- monthly free-tier cap ----------
from datetime import datetime, timezone  # noqa: E402

from app import clock  # noqa: E402
from app.store import Store  # noqa: E402


def cap_settings(tmp_settings, **kw):
    # cap = 0.8 x 1000 = 800 vCPU-s / GiB-s ; estimate per page = 10 s x 2 = 20
    base = dict(remote_free_vcpu_seconds=1000, remote_free_gib_seconds=1000, remote_est_page_seconds=10)
    base.update(kw)
    return dispatcher_settings(tmp_settings, **base)


def fixed_clock(monkeypatch, y, m, d=15):
    monkeypatch.setattr(clock, "now", lambda: datetime(y, m, d, 12, 0, tzinfo=timezone.utc))


def test_usage_recorded_success_and_failure(tmp_settings, monkeypatch):
    fixed_clock(monkeypatch, 2026, 11)
    fr = FakeRemote()
    fr.delay = {0: 0.3}
    fr.fail = {1: [remote.RemoteError("x", retryable=True)]}
    monkeypatch.setattr(remote, "recognize_page", fr)
    with TestClient(create_app(cap_settings(tmp_settings))) as c:
        r, _ = poll(c, submit(c, 2))
        assert r["status"] == "done"
        v, g = c.app.state.store.remote_usage("2026-11")
        # 3 attempts (page0, page1 failed, page1 retry) measured wall seconds x 2 vCPU / x 2 GiB
        assert fr.calls.count(1) == 2
        assert 0.7 <= v < 20 and v == g  # estimate (20 each) was replaced by measurements


def test_cap_reached_runs_local_no_worker_calls(tmp_settings, monkeypatch):
    fixed_clock(monkeypatch, 2026, 11)
    fr = FakeRemote()
    monkeypatch.setattr(remote, "recognize_page", fr)
    s = cap_settings(tmp_settings)
    st = Store(s.data_dir / "app.sqlite")
    st.remote_add("2026-11", 790, 790)  # 790 + 20 > 800
    with TestClient(create_app(s)) as c:
        me = c.get("/api/me", headers=HDR).json()["remote"]
        assert me["capped"] is True and me["month"] == "2026-11" and me["cap_ratio"] == 0.8
        r, _ = poll(c, submit(c, 2))
        assert r["status"] == "done" and fr.calls == []
        assert all(len(p["notes"]) == 8 for p in r["result"]["pages"])  # mock engine ran locally
        assert c.app.state.store.remote_usage("2026-11") == (790, 790)  # nothing reserved or billed


def test_cap_checks_gib_too(tmp_settings, monkeypatch):
    fixed_clock(monkeypatch, 2026, 11)
    fr = FakeRemote()
    monkeypatch.setattr(remote, "recognize_page", fr)
    s = cap_settings(tmp_settings)
    Store(s.data_dir / "app.sqlite").remote_add("2026-11", 0, 790)
    with TestClient(create_app(s)) as c:
        r, _ = poll(c, submit(c, 1))
        assert r["status"] == "done" and fr.calls == []


def test_reservation_prevents_overshoot_in_parallel(tmp_settings, monkeypatch):
    fixed_clock(monkeypatch, 2026, 11)
    fr = FakeRemote()
    fr.delay = {i: 1.5 for i in range(10)}  # all 10 in flight before any settles
    monkeypatch.setattr(remote, "recognize_page", fr)
    # cap 800, est 20/page, already 700 -> room for exactly 5 reservations (700 + 5x20 = 800)
    s = cap_settings(tmp_settings)
    Store(s.data_dir / "app.sqlite").remote_add("2026-11", 700, 700)
    with TestClient(create_app(s)) as c:
        peak = []
        orig = c.app.state.store.remote_add

        def spy(*a, **k):
            ok = orig(*a, **k)
            peak.append(c.app.state.store.remote_usage("2026-11")[0])
            return ok
        c.app.state.store.remote_add = spy
        r, _ = poll(c, submit(c, 10))
        assert r["status"] == "done" and len(r["result"]["pages"]) == 10
        assert len(set(fr.calls)) == 5 and len(fr.calls) == 5  # only 5 pages reached the worker, rest local
        assert max(peak) <= 800 + 1e-6  # reserved totals never exceed the cap, even with 10 in flight
        assert [p["index"] for p in r["result"]["pages"]] == list(range(10))


def test_month_rollover_resets(tmp_settings, monkeypatch):
    fixed_clock(monkeypatch, 2026, 11, 30)
    fr = FakeRemote()
    monkeypatch.setattr(remote, "recognize_page", fr)
    s = cap_settings(tmp_settings)
    Store(s.data_dir / "app.sqlite").remote_add("2026-11", 790, 790)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/me", headers=HDR).json()["remote"]["capped"] is True
        # 2026-12-01 02:00 UTC is still 2026-11-30 in Pacific time: same billing month
        monkeypatch.setattr(clock, "now", lambda: datetime(2026, 12, 1, 2, 0, tzinfo=timezone.utc))
        assert c.get("/api/me", headers=HDR).json()["remote"]["month"] == "2026-11"
        monkeypatch.setattr(clock, "now", lambda: datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc))
        me = c.get("/api/me", headers=HDR).json()["remote"]
        assert me["month"] == "2026-12" and me["capped"] is False and me["vcpu_seconds"] == 0
        r, _ = poll(c, submit(c, 1))
        assert r["status"] == "done" and fr.calls == [0]


def test_usage_persisted_across_store_reopen(tmp_path):
    p = tmp_path / "s.sqlite"
    Store(p).remote_add("2026-11", 12.5, 6.25)
    s2 = Store(p)
    assert s2.remote_usage("2026-11") == (12.5, 6.25) and s2.remote_usage("2026-12") == (0.0, 0.0)
    assert s2.remote_add("2026-11", 100, 100, cap=(50, 50)) is False
    assert s2.remote_usage("2026-11") == (12.5, 6.25)
    s2.remote_add("2026-11", -100, -100)
    assert s2.remote_usage("2026-11") == (0.0, 0.0)  # settle never goes negative


def test_capped_new_jobs_run_one_at_a_time_and_queue_reflects(tmp_settings, monkeypatch):
    fixed_clock(monkeypatch, 2026, 11)
    monkeypatch.setattr(remote, "recognize_page", FakeRemote())
    monkeypatch.setenv("MOCK_ENGINE_DELAY_MS", "800")
    s = cap_settings(tmp_settings, concurrency=4)
    Store(s.data_dir / "app.sqlite").remote_add("2026-11", 790, 790)
    with TestClient(create_app(s)) as c:
        jobs = [submit(c, 1, user=str(u)) for u in (1, 2, 3)]
        time.sleep(0.4)
        sts = [c.get(f"/api/omr/{j}", headers=h).json() for h, j in jobs]
        assert sorted(x["status"] for x in sts) == ["queued", "queued", "running"]
        assert sorted(x["progress"]["queue_position"] for x in sts if x["status"] == "queued") == [1, 2]
        for hj in jobs:
            assert poll(c, hj)[0]["status"] == "done"


def test_me_has_no_remote_when_disabled(tmp_settings):
    with TestClient(create_app(tmp_settings(service_key=SKEY, dev_auth=False))) as c:
        assert c.get("/api/me", headers=HDR).json()["remote"] is None
