"""Upload validation, rasterization and OMR subprocess driver."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections import deque
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps

from .config import Settings
from .progress import MARKER, parse_marker

log = logging.getLogger("spm.pipeline")
Image.MAX_IMAGE_PIXELS = 120_000_000
MAX_PIXELS = 40_000_000
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


class UploadError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


def sniff(head: bytes) -> str | None:
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def validate_upload(path: Path, filename: str, settings: Settings) -> tuple[str, int]:
    """Returns (kind, page_count) where kind is 'pdf' or 'image'. Raises UploadError."""
    if Path(filename).suffix.lower() not in ALLOWED_EXT:
        raise UploadError(415, "unsupported_type")
    with open(path, "rb") as f:
        kind = sniff(f.read(2048))
    if kind is None:
        raise UploadError(415, "unsupported_type")
    if kind == "pdf":
        try:
            doc = fitz.open(path)
        except Exception:
            raise UploadError(422, "invalid_pdf")
        try:
            if doc.needs_pass or doc.is_encrypted:
                raise UploadError(422, "encrypted_pdf")
            n = doc.page_count
        finally:
            doc.close()
        if n < 1:
            raise UploadError(422, "invalid_pdf")
        if n > settings.max_pages:
            raise UploadError(422, "too_many_pages", f"max {settings.max_pages}")
        return "pdf", n
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception:
        raise UploadError(422, "invalid_image")
    return "image", 1


def normalize_image(src: Path, dst: Path) -> tuple[int, int]:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg
        im = im.convert("RGB")
        im.save(dst, "PNG")
        return im.size


def rasterize_page(pdf: Path, index: int, dst: Path, dpi: int) -> dict:
    doc = fitz.open(pdf)
    try:
        page = doc[index]
        r = page.rect
        zoom = dpi / 72.0
        if r.width * zoom * r.height * zoom > MAX_PIXELS:
            zoom = (MAX_PIXELS / (r.width * r.height)) ** 0.5
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(str(dst))
        return {"w": pix.width, "h": pix.height, "pt_w": r.width, "pt_h": r.height, "zoom": zoom}
    finally:
        doc.close()


def _kill_tree(p: subprocess.Popen):
    try:
        if os.name == "nt":  # run_engine.py may re-exec itself (UTF-8 mode): kill the whole tree
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        p.kill()
    except Exception:  # noqa: BLE001
        pass


def run_engine(settings: Settings, engine: str, image: Path, out: Path, on_progress=None) -> dict:
    """Runs one engine subprocess.  stderr is read on a thread: `@@progress` marker lines go to
    on_progress(stage, p); other stderr lines are kept (tail) for error reporting."""
    cmd = [settings.engine_python(engine), str(settings.run_engine), "--engine", engine,
           "--image", str(image), "--out", str(out)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, cwd=str(settings.root))
    tail: deque[str] = deque(maxlen=50)

    def pump():
        for raw in iter(p.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            m = parse_marker(line)
            if m is not None:
                if on_progress:
                    try:
                        on_progress(*m)
                    except Exception:  # noqa: BLE001  never let progress reporting break a job
                        log.exception("progress callback failed")
            elif line.strip() and not line.startswith(MARKER):
                tail.append(line)

    t = threading.Thread(target=pump, daemon=True, name="omr-stderr")
    t.start()
    try:
        rc = p.wait(timeout=settings.timeout_per_page_s)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        p.wait()
        t.join(5)
        raise RuntimeError("engine_timeout")
    t.join(10)
    if rc != 0:
        last = tail[-1][:200] if tail else f"exit {rc}"
        log.warning("engine %s failed rc=%s: %s", engine, rc, last)
        raise RuntimeError("engine_failed: " + last)
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("engine_bad_output")


def merge_page(i: int, raw: dict, pg: dict | None, size: tuple[int, int]) -> dict:
    pre = f"p{i}"
    staves = []
    for s in raw.get("staves") or []:
        s = dict(s)
        s["id"] = pre + str(s.get("id"))
        staves.append(s)
    notes = []
    for n in raw.get("notes") or []:
        n = dict(n)
        n["id"] = pre + str(n.get("id"))
        n["staff"] = pre + str(n["staff"]) if n.get("staff") is not None else None
        notes.append(n)
    return {
        "index": i, "image_url": None,  # filled by caller
        "width_px": raw.get("width_px") or size[0], "height_px": raw.get("height_px") or size[1],
        "pdf_width_pt": round(pg["pt_w"], 2) if pg else None,
        "pdf_height_pt": round(pg["pt_h"], 2) if pg else None,
        "staves": staves, "notes": notes,
        "elapsed_ms": raw.get("elapsed_ms"), "warnings": list(raw.get("warnings") or []),
    }
