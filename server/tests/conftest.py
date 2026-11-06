import functools
import io
import sys
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

from app import tokens  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SECRET = "test-secret-not-real"


@functools.lru_cache(maxsize=None)
def make_pdf(pages=1, w=595.0, h=842.0, tag="x") -> bytes:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=w, height=h)
        p.insert_text((72, 100), f"hello {tag} page {i}", fontsize=14)
        for k in range(5):
            p.draw_line((72, 200 + k * 6), (500, 200 + k * 6))
    b = doc.tobytes()
    doc.close()
    return b


def make_png(w=400, h=300, color=(255, 255, 255), fmt="PNG", mode="RGB") -> bytes:
    im = Image.new(mode, (w, h), color if mode == "RGB" else (0, 0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def tmp_settings(tmp_path):
    def _mk(**kw):
        base = dict(data_dir=tmp_path / "data", auth_secret=SECRET, session_secret="s" * 32, dev_auth=True,
                    admin_token="adm", engines={"default": "mock"}, web_dir=tmp_path / "noweb",
                    run_engine=SERVER / "app" / "mock_engine.py", timeout_per_page_s=60)
        base.update(kw)
        return Settings(**base)
    return _mk


@pytest.fixture
def client(tmp_settings):
    with TestClient(create_app(tmp_settings()), follow_redirects=False) as c:
        yield c


@pytest.fixture
def login(client):
    def _l(name="tester", quota=5):
        r = client.get("/auth/dev-login", params={"name": name, "quota": quota})
        assert r.status_code == 303
        return client
    return _l


def mint(sub=12, name="王小明", quota=5, ttl=300, nonce=None, secret=SECRET, iss="storypathmusic.com"):
    now = int(time.time())
    return tokens.sign(secret, {"iss": iss, "sub": sub, "name": name, "quota": quota, "iat": now,
                                "exp": now + ttl, "nonce": nonce or f"n{time.time_ns()}abcdef"})


def wait_done(client, job_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/api/omr/{job_id}").json()
        if r["status"] in ("done", "error"):
            return r
        time.sleep(0.1)
    raise AssertionError("job timeout")
