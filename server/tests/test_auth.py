import time

from conftest import SECRET, mint
from app import tokens


def test_verify_valid():
    c = tokens.verify(SECRET, mint(sub=7, quota=3))
    assert c["sub"] == 7 and c["quota"] == 3 and c["name"] == "王小明"


def test_verify_bad_sig():
    t = mint(secret="other-secret")
    try:
        tokens.verify(SECRET, t)
        assert False
    except tokens.TokenError as e:
        assert e.reason == "bad_signature"


def test_verify_tampered_payload():
    t = mint(quota=1)
    p, s = t.split(".")
    forged = tokens.b64url_encode(b'{"iss":"storypathmusic.com","sub":1,"quota":999,"exp":9999999999,"nonce":"abcdefghij"}')
    try:
        tokens.verify(SECRET, forged + "." + s)
        assert False
    except tokens.TokenError as e:
        assert e.reason == "bad_signature"


def test_verify_expired():
    try:
        tokens.verify(SECRET, mint(ttl=-5))
        assert False
    except tokens.TokenError as e:
        assert e.reason == "expired"


def test_verify_malformed():
    for t in ["", "abc", "a.b.c", "!!.!!"]:
        try:
            tokens.verify(SECRET, t)
            assert False
        except tokens.TokenError:
            pass


def test_callback_flow_and_replay(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me").json() == {"error": "unauthenticated"}
    t = mint(sub=12, quota=4)
    r = client.get("/auth/callback", params={"token": t, "next": "/x"})
    assert r.status_code == 303 and r.headers["location"] == "/x"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    me = client.get("/api/me").json()
    assert me["user"] == {"id": 12, "name": "王小明"}
    assert me["quota"]["limit"] == 4 and me["quota"]["remaining"] == 4
    # replay of the same token is rejected
    r2 = client.get("/auth/callback", params={"token": t})
    assert r2.status_code == 400 and r2.json()["reason"] == "replayed"


def test_callback_rejects_bad_and_open_redirect(client):
    r = client.get("/auth/callback", params={"token": mint(secret="nope")})
    assert r.status_code == 400
    r = client.get("/auth/callback", params={"token": mint(), "next": "//evil.com"})
    assert r.headers["location"] == "/"


def test_login_redirect_to_wp(tmp_settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(tmp_settings(wp_base_url="http://wp.test")), follow_redirects=False) as c:
        r = c.get("/auth/login", params={"next": "/a?b=1"})
        loc = r.headers["location"]
        assert loc.startswith("http://wp.test/?spm_tool_auth=1&return=")
        assert "%2Fauth%2Fcallback%3Fnext%3D" in loc


def test_logout(login, client):
    login()
    assert client.get("/api/me").status_code == 200
    client.post("/auth/logout")
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401


def test_admin_usage(client, login):
    assert client.get("/admin/usage").status_code == 403
    assert client.get("/admin/usage", params={"token": "adm"}).status_code == 200
    assert client.get("/admin/usage", headers={"X-Admin-Token": "adm"}).json()["users"] == []


def test_static_served_no_store(tmp_settings, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    with TestClient(create_app(tmp_settings(web_dir=web)), follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code == 200 and "hi" in r.text and r.headers["cache-control"] == "no-store"
        assert c.get("/api/me").status_code == 401  # API routes not shadowed by static mount


def test_me_exposes_limits(client, login):
    login()
    lim = client.get("/api/me").json()["limits"]
    assert lim["max_upload_mb"] > 0 and lim["max_pages"] > 0
    assert lim["retention_hours"] == 24 and lim["est_seconds_per_page"] > 0
