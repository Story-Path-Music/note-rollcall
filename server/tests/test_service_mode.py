"""服務模式（SERVICE_KEY）：只信任帶正確 X-SPM-Service-Key 的請求；額度由上游管；不提供登入與靜態前端。"""
import pytest
from fastapi.testclient import TestClient
from urllib.parse import quote

from conftest import make_pdf, make_png, wait_done
from app.config import Settings
from app.main import create_app

KEY = "k" * 32


def hdr(uid=12, name="王小明", key=KEY):
    return {"X-SPM-Service-Key": key, "X-SPM-User-Id": str(uid), "X-SPM-User-Name": quote(name)}


@pytest.fixture
def svc(tmp_settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>app</html>", encoding="utf-8")
    s = tmp_settings(service_key=KEY, web_dir=web)
    with TestClient(create_app(s), follow_redirects=False) as c:
        yield c


def test_requires_key(svc):
    for h in ({}, {"X-SPM-Service-Key": "wrong" * 8, "X-SPM-User-Id": "1"}, {"X-SPM-User-Id": "1"}):
        r = svc.get("/api/me", headers=h)
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert svc.post("/api/omr", files={"file": ("a.pdf", make_pdf(1))}).status_code == 401
    assert svc.post("/api/export", json={"job_id": "0" * 32}).status_code == 401
    assert svc.get("/api/omr/" + "0" * 32).status_code == 401


def test_bad_user_header(svc):
    for uid in ("", "abc", "0", "-3", "1.5"):
        h = hdr()
        h["X-SPM-User-Id"] = uid
        assert svc.get("/api/me", headers=h).status_code == 400


def test_me_has_no_quota_and_decodes_name(svc):
    r = svc.get("/api/me", headers=hdr(7, "測試者"))
    assert r.status_code == 200
    b = r.json()
    assert b["user"] == {"id": 7, "name": "測試者"} and b["quota"] is None and b["dev"] is False
    assert b["limits"]["max_pages"] == 10


def test_login_session_admin_and_static_disabled(svc):
    for m, p in [("get", "/auth/login"), ("get", "/auth/callback"), ("post", "/auth/logout"),
                 ("get", "/auth/dev-login"), ("get", "/admin/usage"), ("get", "/"), ("get", "/index.html")]:
        assert getattr(svc, m)(p).status_code == 404, p
    assert svc.get("/healthz").json() == {"ok": True}
    # cookie from a would-be session is ignored
    svc.cookies.set("spm_session", "x")
    assert svc.get("/api/me").status_code == 401


def test_job_flow_and_no_backend_quota(svc):
    h = hdr(21)
    ids = []
    for i in range(7):  # 超過舊的每日 5 份：服務模式不在後端擋
        r = svc.post("/api/omr", files={"file": ("a.pdf", make_pdf(1, tag=f"t{i}"))}, headers=h)
        assert r.status_code == 202, r.text
        ids.append(r.json()["job_id"])
    for jid in ids:
        j = wait_done_h(svc, jid, h)
        assert j["status"] == "done"
    assert svc.get(f"/api/omr/{ids[0]}/page/0.png", headers=h).status_code == 200


def wait_done_h(c, jid, h, timeout=30):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/omr/{jid}", headers=h).json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.1)
    raise AssertionError("timeout")


def test_same_file_same_user_is_cached_and_ownership(svc):
    data = make_png()
    a = svc.post("/api/omr", files={"file": ("a.png", data)}, headers=hdr(1)).json()
    wait_done_h(svc, a["job_id"], hdr(1))
    again = svc.post("/api/omr", files={"file": ("a.png", data)}, headers=hdr(1)).json()
    assert again == {"job_id": a["job_id"], "cached": True}
    # 另一個使用者看不到
    assert svc.get(f"/api/omr/{a['job_id']}", headers=hdr(2)).status_code == 404
    assert svc.get(f"/api/omr/{a['job_id']}/page/0.png", headers=hdr(2)).status_code == 404


def test_upload_limits_still_apply(svc):
    h = hdr(3)
    assert svc.post("/api/omr", files={"file": ("a.txt", b"hello")}, headers=h).status_code == 415
    assert svc.post("/api/omr", files={"file": ("a.pdf", make_pdf(11))}, headers=h).status_code == 422


def test_constant_time_compare_used():
    import inspect, app.main as m
    assert "compare_digest" in inspect.getsource(m)


def test_short_key_rejected(tmp_settings):
    with pytest.raises(ValueError):
        tmp_settings(service_key="short")


def test_dev_auth_unaffected_without_key(client, login):
    login()
    assert client.get("/api/me").json()["quota"]["limit"] == 5
    assert client.get("/auth/login").status_code == 302
