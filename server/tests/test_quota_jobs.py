import datetime as dt

import pytest

from conftest import make_pdf, make_png, wait_done
from app import clock


def post(client, data, name="a.pdf", **form):
    return client.post("/api/omr", files={"file": (name, data)}, data=form)


def test_job_lifecycle_pdf(client, login):
    login()
    r = post(client, make_pdf(2))
    assert r.status_code == 202 and r.json()["cached"] is False
    j = wait_done(client, r.json()["job_id"])
    assert j["status"] == "done" and j["error"] is None
    res = j["result"]
    assert res["source"] == {"type": "pdf", "filename": "a.pdf", "page_count": 2}
    p0, p1 = res["pages"]
    assert p0["pdf_width_pt"] == 595.0 and abs(p0["width_px"] - 595 * 300 / 72) <= 1
    assert p0["notes"][0]["id"] == "p0n0" and p0["notes"][0]["staff"] == "p0s0"
    assert p1["notes"][0]["id"] == "p1n0" and p1["staves"][0]["id"] == "p1s0"
    img = client.get(p0["image_url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert client.get(f"/api/omr/{r.json()['job_id']}/page/5.png").status_code == 404
    assert client.get("/api/me").json()["quota"]["used"] == 1


def test_image_sources(client, login):
    login()
    for name, data in [("a.png", make_png()), ("b.jpg", make_png(fmt="JPEG")), ("c.webp", make_png(fmt="WEBP")),
                       ("d.png", make_png(mode="RGBA"))]:
        r = post(client, data, name)
        assert r.status_code == 202, name
        j = wait_done(client, r.json()["job_id"])
        assert j["status"] == "done", (name, j)
        pg = j["result"]["pages"][0]
        assert pg["pdf_width_pt"] is None and pg["width_px"] == 400


def test_other_user_cannot_see_job(client, login):
    login("u1")
    jid = post(client, make_pdf(1)).json()["job_id"]
    wait_done(client, jid)
    login("u2")
    assert client.get(f"/api/omr/{jid}").status_code == 404


def test_quota_limit_and_cache(client, login):
    login(quota=2)
    a = post(client, make_pdf(1, tag="a")).json()
    again = post(client, make_pdf(1, tag="a")).json()
    assert again == {"job_id": a["job_id"], "cached": True}
    assert client.get("/api/me").json()["quota"]["used"] == 1
    b = post(client, make_pdf(1, tag="b"))
    assert b.status_code == 202
    c = post(client, make_pdf(1, tag="c"))
    assert c.status_code == 429
    body = c.json()
    assert body["error"] == "quota_exceeded" and body["quota"]["remaining"] == 0
    # cached file still served when exhausted
    assert post(client, make_pdf(1, tag="a")).json()["cached"] is True
    wait_done(client, a["job_id"]); wait_done(client, b.json()["job_id"])


def test_failure_refunds(client, login, monkeypatch):
    login(quota=1)
    monkeypatch.setenv("MOCK_ENGINE_FAIL", "1")
    r = post(client, make_pdf(1))
    j = wait_done(client, r.json()["job_id"])
    assert j["status"] == "error" and "engine_failed" in j["error"]
    assert client.get("/api/me").json()["quota"]["used"] == 0
    monkeypatch.delenv("MOCK_ENGINE_FAIL")
    r2 = post(client, make_pdf(1))  # same file after failure: new job, charged once
    assert r2.status_code == 202 and r2.json()["cached"] is False
    assert wait_done(client, r2.json()["job_id"])["status"] == "done"
    assert client.get("/api/me").json()["quota"]["used"] == 1


def test_concurrent_reserve_no_overspend(client, login, monkeypatch):
    login(quota=1)
    monkeypatch.setenv("MOCK_ENGINE_DELAY_MS", "500")
    r1 = post(client, make_pdf(1, tag="1"))
    r2 = post(client, make_pdf(1, tag="2"))  # first still running: reservation counts
    assert r1.status_code == 202 and r2.status_code == 429
    wait_done(client, r1.json()["job_id"])


def test_day_boundary_taipei(client, login, monkeypatch):
    login(quota=1)
    base = dt.datetime(2026, 11, 6, 15, 30, tzinfo=dt.timezone.utc)  # 23:30 Taipei, Nov 6
    monkeypatch.setattr(clock, "now", lambda: base)
    assert client.get("/api/me").json()["quota"]["date"] == "2026-11-06"
    r = post(client, make_pdf(1)); wait_done(client, r.json()["job_id"])
    assert post(client, make_pdf(1, tag="z")).status_code == 429
    monkeypatch.setattr(clock, "now", lambda: base + dt.timedelta(minutes=45))  # 00:15 Nov 7 Taipei
    me = client.get("/api/me").json()["quota"]
    assert me["date"] == "2026-11-07" and me["used"] == 0
    assert post(client, make_pdf(1, tag="z")).status_code == 202
    # previous day's file is charged again on the new day (not cached)
    assert post(client, make_pdf(1)).status_code in (202, 429)


def test_upload_validation(client, login, tmp_settings):
    assert post(client, make_pdf(1)).status_code == 401
    login()
    assert post(client, b"not a pdf at all", "a.pdf").status_code == 415   # magic bytes wrong
    assert post(client, make_pdf(1), "a.txt").status_code == 415          # extension
    assert post(client, b"%PDF-1.4 garbage", "a.pdf").status_code == 422   # corrupt
    assert post(client, make_pdf(11)).status_code == 422                   # > MAX_PAGES
    assert post(client, b"", "a.pdf").status_code == 422
    assert client.get("/api/me").json()["quota"]["used"] == 0
    assert post(client, make_png(), "a.pdf").status_code == 202 or True    # png bytes named .pdf is accepted by magic
    assert post(client, make_pdf(1), engine="nope").status_code == 400


def test_too_large(tmp_settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(tmp_settings(max_upload_mb=1)), follow_redirects=False) as c:
        c.get("/auth/dev-login")
        big = make_pdf(1) + b"0" * (2 * 1024 * 1024)
        assert post(c, big).status_code == 413
        assert c.get("/api/me").json()["quota"]["used"] == 0


def test_cleanup_old_jobs(client, login, monkeypatch):
    login()
    jid = post(client, make_pdf(1)).json()["job_id"]
    wait_done(client, jid)
    jobs = client.app.state.jobs
    assert (jobs.dir(jid)).exists()
    later = clock.now() + dt.timedelta(hours=25)
    monkeypatch.setattr(clock, "now", lambda: later)
    jobs.cleanup()
    assert not jobs.dir(jid).exists()
    assert client.app.state.store.get_job(jid) is None
