import io

import pymupdf as fitz
from PIL import Image

from conftest import make_pdf, make_png, wait_done


def run(client, data, name):
    r = client.post("/api/omr", files={"file": (name, data)})
    assert r.status_code == 202
    j = wait_done(client, r.json()["job_id"])
    assert j["status"] == "done"
    return r.json()["job_id"], j["result"]


LABELS = [{"page": 0, "x": 600.0, "y": 500.0, "text": "Do", "dots_above": 0, "dots_below": 1},
          {"page": 1, "x": 700.0, "y": 600.0, "text": "#4", "dots_above": 2, "dots_below": 0}]
STYLE = {"font_size_px": 30, "color": "#d0021b"}


def test_export_pdf_preserves_size_and_draws(client, login):
    login()
    src = make_pdf(2, w=500, h=700)
    jid, res = run(client, src, "s.pdf")
    r = client.post("/api/export", json={"job_id": jid, "format": "pdf", "page": None, "style": STYLE, "labels": LABELS})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    doc = fitz.open(stream=r.content, filetype="pdf")
    assert doc.page_count == 2
    assert (doc[0].rect.width, doc[0].rect.height) == (500, 700)
    t0, t1 = doc[0].get_text(), doc[1].get_text()
    assert "Do" in t0 and "hello x page 0" in t0          # original vector text kept
    assert "#4" in t1 and "hello x page 1" in t1
    # position: label centered at px (600,500) -> pt via 500/width_px
    scale = 500 / res["pages"][0]["width_px"]
    box = [b for b in doc[0].get_text("words") if b[4] == "Do"][0]
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    assert abs(cx - 600 * scale) < 2 and abs(cy - 500 * scale) < 4
    assert len(doc[0].get_drawings()) > 5   # staff lines + octave dot


def test_export_png_from_pdf(client, login):
    login()
    jid, res = run(client, make_pdf(2), "s.pdf")
    pg = res["pages"][1]
    r = client.post("/api/export", json={"job_id": jid, "format": "png", "page": 1, "style": STYLE, "labels": LABELS})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    im = Image.open(io.BytesIO(r.content))
    assert im.size == (pg["width_px"], pg["height_px"])
    px = im.convert("RGB").load()
    red = sum(1 for x in range(650, 750) for y in range(560, 640)
              if px[x, y][0] > 150 and px[x, y][1] < 90)
    assert red > 20
    assert client.post("/api/export", json={"job_id": jid, "format": "png", "labels": []}).status_code == 422


def test_export_image_source(client, login):
    login()
    jid, res = run(client, make_png(1000, 800), "s.png")
    lab = [{"page": 0, "x": 500, "y": 400, "text": "C#", "dots_above": 1, "dots_below": 0}]
    r = client.post("/api/export", json={"job_id": jid, "format": "png", "page": 0, "style": STYLE, "labels": lab})
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert im.size == (1000, 800)
    px = im.load()
    assert sum(1 for x in range(470, 530) for y in range(370, 430) if px[x, y][0] > 150 and px[x, y][1] < 90) > 20
    r = client.post("/api/export", json={"job_id": jid, "format": "pdf", "style": STYLE, "labels": lab})
    doc = fitz.open(stream=r.content, filetype="pdf")
    assert doc.page_count == 1 and "C#" in doc[0].get_text()
    bad = client.post("/api/export", json={"job_id": jid, "format": "pdf", "style": STYLE,
                                           "labels": [{"page": 3, "x": 1, "y": 1, "text": "C"}]})
    assert bad.status_code == 422


def test_export_per_label_font_size(client, login):
    """a label may carry its own font_size_px (rows shrunk locally); missing -> style.font_size_px"""
    login()
    jid, res = run(client, make_pdf(1, w=500, h=700), "s.pdf")
    labs = [{"page": 0, "x": 200.0, "y": 300.0, "text": "Do", "font_size_px": 15},
            {"page": 0, "x": 600.0, "y": 300.0, "text": "Re"}]
    r = client.post("/api/export", json={"job_id": jid, "format": "pdf", "style": {"font_size_px": 30, "color": "#000000"},
                                         "labels": labs})
    assert r.status_code == 200
    doc = fitz.open(stream=r.content, filetype="pdf")
    scale = 500 / res["pages"][0]["width_px"]
    sizes = {}
    for blk in doc[0].get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                sizes[sp["text"].strip()] = sp["size"]
    assert abs(sizes["Do"] - 15 * scale) < 0.2 and abs(sizes["Re"] - 30 * scale) < 0.2
    bad = client.post("/api/export", json={"job_id": jid, "format": "pdf", "labels": [{"x": 1, "y": 1, "font_size_px": -3}]})
    assert bad.status_code == 422
    # PNG path on an image source honours it too (smaller font -> fewer ink pixels)
    jid2, _ = run(client, make_png(1000, 800), "s.png")
    def ink(fs):
        rr = client.post("/api/export", json={"job_id": jid2, "format": "png", "page": 0,
                                              "style": {"font_size_px": 40, "color": "#ff0000"},
                                              "labels": [{"page": 0, "x": 500, "y": 400, "text": "88", **({"font_size_px": fs} if fs else {})}]})
        assert rr.status_code == 200
        px = Image.open(io.BytesIO(rr.content)).convert("RGB").load()
        return sum(1 for x in range(440, 560) for y in range(350, 450) if px[x, y][0] > 150 and px[x, y][1] < 90)
    assert ink(16) < ink(None) * 0.6


def test_export_auth_and_state(client, login):
    assert client.post("/api/export", json={"job_id": "x" * 32, "labels": []}).status_code == 401
    login()
    assert client.post("/api/export", json={"job_id": "0" * 32, "labels": []}).status_code == 404


def test_export_accidental_parts_draw_glyph(client, login):
    """含升降記號的標記：parts 分段畫，PDF 內嵌 SPM Accidentals 字型、PNG 多出字形像素；不合法的 parts 回 422。"""
    login()
    jid, _ = run(client, make_pdf(1, w=500, h=700), "s.pdf")
    parts = [{"kind": "acc", "name": "sharp", "x": -0.4, "size": 0.8, "dy": -0.1}, {"kind": "text", "text": "4", "x": 0.2}]
    lab = [{"page": 0, "x": 600.0, "y": 500.0, "text": "#4", "parts": parts}]
    r = client.post("/api/export", json={"job_id": jid, "format": "pdf", "page": None, "style": STYLE, "labels": lab})
    assert r.status_code == 200, r.text
    doc = fitz.open(stream=r.content, filetype="pdf")
    names = [f[3] for f in doc[0].get_fonts()]
    assert any("SPM" in n for n in names), names
    bad = [{"page": 0, "x": 1, "y": 1, "text": "#4", "parts": [{"kind": "acc", "name": "bogus"}]}]
    assert client.post("/api/export", json={"job_id": jid, "format": "pdf", "style": STYLE, "labels": bad}).status_code == 422
