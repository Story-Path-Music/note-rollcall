import subprocess
import sys
import time

from conftest import make_pdf, make_png, wait_done, SERVER
from app import progress as prog
from app.jobs import JobManager


def post(client, data, name="a.pdf"):
    return client.post("/api/omr", files={"file": (name, data)})


def test_parse_marker():
    assert prog.parse_marker('@@progress {"stage":"detect","p":0.4}') == ("detect", 0.4)
    assert prog.parse_marker('  @@progress {"stage":"post","p":2}\n') == ("post", 1.0)  # clamped
    assert prog.parse_marker('@@progress {"stage":"transcribe","p":-1}') == ("transcribe", 0.0)
    assert prog.parse_marker('@@progress {"stage":"done","p":1}') == ("post", 1.0)  # engine done = page post done
    for bad in ['', 'homr: warning', '@@progress {not json', '@@progress {"stage":"nope","p":1}',
                '@@progress {"stage":"detect"}', '@@progress {"stage":"detect","p":"x"}',
                '@@progress {"stage":"detect","p":NaN}', '@@progress []']:
        assert prog.parse_marker(bad) is None, bad


def test_weights_sum_to_one_and_page_fraction_increases():
    assert abs(sum(prog.STAGE_WEIGHTS.values()) - 1.0) < 1e-9
    seq = [(s, p / 4) for s in prog.STAGES for p in range(5)]
    vals = [prog.page_fraction(s, p) for s, p in seq]
    assert vals == sorted(vals) and vals[0] == 0.0 and abs(vals[-1] - 1.0) < 1e-9


def test_fraction_monotonic_across_pages():
    tr = prog.JobProgress(3)
    seen = []
    for page in (1, 2, 3):
        tr.begin_page(page)
        seen.append(tr.fraction)
        for st in prog.STAGES:
            for p in (0.0, 0.5, 1.0):
                tr.mark(st, p)
                seen.append(tr.fraction)
        tr.mark("detect", 0.1)  # an out-of-order (backwards) marker must not move it back
        seen.append(tr.fraction)
    assert seen == sorted(seen)
    assert seen[-1] <= prog.MAX_RUNNING_FRACTION < 1.0
    pg, stage, frac, _ = tr.snapshot()
    assert pg == 3 and frac == seen[-1]


def test_eta():
    assert prog.eta_seconds(0.0, 0, 2, 60) == 120  # nothing known: configured estimate
    assert prog.eta_seconds(0.05, 3, 1, 60) == 57
    assert prog.eta_seconds(0.5, 30, 1, 60) == 30  # elapsed-based
    assert prog.eta_seconds(1.0, 30, 1, 60) == 0
    assert prog.eta_seconds(0.0, 0, 1, 0) is None


def test_mock_engine_emits_stages(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(make_png())
    p = subprocess.run([sys.executable, str(SERVER / "app" / "mock_engine.py"), "--image", str(img),
                        "--out", str(tmp_path / "o.json")], capture_output=True, text=True)
    assert p.returncode == 0
    marks = [prog.parse_marker(l) for l in p.stderr.splitlines() if l.startswith("@@progress")]
    assert all(marks)
    stages = []
    for s, _ in marks:
        if not stages or stages[-1] != s:
            stages.append(s)
    assert stages == ["detect", "transcribe", "post"]
    assert p.stderr.splitlines()[-1] == '@@progress {"stage":"done","p":1.0}'
    assert (tmp_path / "o.json").exists()


def test_job_progress_field_done(client, login):
    login()
    jid = post(client, make_pdf(2)).json()["job_id"]
    j = wait_done(client, jid)
    assert j["progress"] == {"page": 2, "pages": 2, "stage": "done", "fraction": 1.0,
                             "queue_position": 0, "eta_seconds": 0}


def test_progress_while_running_and_queue_position(tmp_settings, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.setenv("MOCK_ENGINE_DELAY_MS", "1500")
    with TestClient(create_app(tmp_settings()), follow_redirects=False) as c:
        c.get("/auth/dev-login", params={"name": "t", "quota": 9})
        a = post(c, make_pdf(2, tag="a")).json()["job_id"]
        b = post(c, make_pdf(1, tag="b")).json()["job_id"]
        d = post(c, make_pdf(1, tag="c")).json()["job_id"]
        fr, stages, qb, qd = [], set(), [], []
        t0 = time.time()
        while time.time() - t0 < 30:
            ja, jb, jd = (c.get(f"/api/omr/{x}").json() for x in (a, b, d))
            pa = ja["progress"]
            assert set(pa) == {"page", "pages", "stage", "fraction", "queue_position", "eta_seconds"}
            fr.append(pa["fraction"])
            stages.add(pa["stage"])
            if jb["status"] == "queued":
                qb.append(jb["progress"]["queue_position"])
                assert jb["progress"]["stage"] == "queued" and jb["progress"]["fraction"] == 0.0
            if jd["status"] == "queued":
                qd.append(jd["progress"]["queue_position"])
            if ja["status"] == "running":
                assert pa["queue_position"] == 0
                assert isinstance(pa["eta_seconds"], int) and pa["eta_seconds"] >= 0
            if jd["status"] == "done":
                break
            time.sleep(0.05)
        assert fr == sorted(fr) and fr[-1] == 1.0
        assert {"detect", "transcribe"} <= stages and "done" in stages
        assert qb and set(qb) == {1} and qd and qd[0] == 2 and qd == sorted(qd, reverse=True)
        assert 0 in qd or qd[-1] >= 1  # position never increases


def test_store_queue_position(tmp_path):
    from app.store import Store
    st = Store(tmp_path / "s.db")
    for i, status in enumerate(["running", "queued", "queued", "done", "queued"]):
        st.create_job(f"j{i}", 1, "mock", "pdf", "x.pdf", 2)
        st.update_job(f"j{i}", status=status)
    assert [st.queue_position(f"j{i}") for i in range(5)] == [0, 1, 2, 0, 3]
    assert st.pages_ahead("j2") == 4
