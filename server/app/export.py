"""Draw labels onto the original PDF / image."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps

CAP = 0.36  # half cap-height as fraction of font size (vertical centering of digits/caps)
ACC_FONT = Path(__file__).resolve().parents[2] / "web" / "fonts" / "SPMAccidentals.ttf"  # Bravura 子集（SIL OFL 1.1）
ACC_CP = {"flat": 0xE260, "natural": 0xE261, "sharp": 0xE262, "doubleSharp": 0xE263, "doubleFlat": 0xE264}  # SMuFL 碼位
FONT_CANDIDATES = [
    "DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


class ExportError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def parse_color(s: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (s or "").strip())
    if not m:
        m2 = re.fullmatch(r"#?([0-9a-fA-F]{3})", (s or "").strip())
        if not m2:
            return (208, 2, 27)
        h = "".join(c * 2 for c in m2.group(1))
    else:
        h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _safe_text(t) -> str:
    return "".join(c for c in str(t) if 32 <= ord(c) < 127)[:12]


def _parts(lb: dict) -> list[dict]:
    """分段排版（含升降記號）；沒有或不合法就回空 list，呼叫端改畫整串 text。"""
    ps = lb.get("parts")
    if not isinstance(ps, list):
        return []
    out = []
    for p in ps[:4]:
        if not isinstance(p, dict):
            return []
        try:
            out.append({"kind": p.get("kind"), "text": p.get("text", ""), "name": p.get("name"),
                        "x": float(p.get("x") or 0), "size": float(p.get("size") or 0), "dy": float(p.get("dy") or 0)})
        except (TypeError, ValueError):
            return []
    return out


def _label_fs(lb: dict, default: float) -> float:
    try:
        v = float(lb.get("font_size_px") or 0)
    except (TypeError, ValueError):
        v = 0.0
    return v if 0 < v <= 2000 else default


def _draw_pdf_page(page: fitz.Page, labels: list, scale: float, fs_px: float, color):
    """labels in page-pixel coords; scale = pt per px. Handles rotated pages."""
    col = tuple(c / 255 for c in color)
    rot = page.rotation
    der = page.derotation_matrix

    def P(x, y):
        return fitz.Point(x, y) * der if rot else fitz.Point(x, y)

    for lb in labels:
        text = _safe_text(lb.get("text", ""))
        cx, cy = float(lb["x"]) * scale, float(lb["y"]) * scale
        fs = max(1.0, _label_fs(lb, fs_px) * scale)  # per-label size (shrunk rows) or the global one
        parts = _parts(lb)
        if parts:
            for p in parts:
                if p["kind"] == "acc":
                    cp = ACC_CP.get(p.get("name"))
                    if cp is None or not ACC_FONT.exists():
                        continue
                    page.insert_font(fontname="spmacc", fontfile=str(ACC_FONT))
                    page.insert_text(P(cx + p["x"] * fs, cy + p["dy"] * fs), chr(cp), fontname="spmacc",
                                     fontsize=max(1.0, p["size"] * fs), color=col, rotate=rot)
                else:
                    t = _safe_text(p.get("text", ""))
                    if t:
                        w = fitz.get_text_length(t, fontname="helv", fontsize=fs)
                        page.insert_text(P(cx + p["x"] * fs - w / 2, cy + CAP * fs), t, fontname="helv", fontsize=fs,
                                         color=col, rotate=rot)
        elif text:
            w = fitz.get_text_length(text, fontname="helv", fontsize=fs)
            page.insert_text(P(cx - w / 2, cy + CAP * fs), text, fontname="helv", fontsize=fs,
                             color=col, rotate=rot)
        r = fs * 0.075
        step = fs * 0.2
        for k in range(max(0, min(int(lb.get("dots_above") or 0), 4))):
            c = P(cx, cy - CAP * fs - fs * 0.22 - k * step)
            page.draw_circle(c, r, color=col, fill=col, width=0)
        for k in range(max(0, min(int(lb.get("dots_below") or 0), 4))):
            c = P(cx, cy + CAP * fs + fs * 0.22 + k * step)
            page.draw_circle(c, r, color=col, fill=col, width=0)


def _by_page(labels: list) -> dict[int, list]:
    out: dict[int, list] = {}
    for lb in labels:
        try:
            out.setdefault(int(lb.get("page", 0)), []).append(lb)
        except (TypeError, ValueError):
            raise ExportError(422, "bad_label")
    return out


def _page_px(result: dict, i: int) -> dict:
    return result["pages"][i]


def export_pdf_source(src: Path, result: dict, labels: list, fs_px: float, color, fmt: str,
                      page: int | None, dpi: int) -> tuple[bytes, str]:
    groups = _by_page(labels)
    n = len(result["pages"])
    if any(i < 0 or i >= n for i in groups):
        raise ExportError(422, "bad_page")
    doc = fitz.open(src)
    try:
        for i, lbs in groups.items():
            pg = _page_px(result, i)
            scale = pg["pdf_width_pt"] / pg["width_px"]
            _draw_pdf_page(doc[i], lbs, scale, fs_px, color)
        if fmt == "pdf":
            return doc.tobytes(garbage=1, deflate=True), "application/pdf"
        if page is None or page < 0 or page >= n:
            raise ExportError(422, "page_required")
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        return pix.tobytes("png"), "image/png"
    finally:
        doc.close()


def _font(size: int):
    for c in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _acc_font(size: int):
    return ImageFont.truetype(str(ACC_FONT), size)


def export_image_source(png: Path, result: dict, labels: list, fs_px: float, color, fmt: str) -> tuple[bytes, str]:
    groups = _by_page(labels)
    if any(i != 0 for i in groups):
        raise ExportError(422, "bad_page")
    lbs = groups.get(0, [])
    if fmt == "png":
        im = Image.open(png).convert("RGB")
        d = ImageDraw.Draw(im)
        fonts: dict[int, ImageFont.FreeTypeFont] = {}
        for lb in lbs:
            fs = max(4, round(_label_fs(lb, fs_px)))
            f = fonts.get(fs) or fonts.setdefault(fs, _font(fs))
            r = fs * 0.075
            step = fs * 0.2
            text = _safe_text(lb.get("text", ""))
            x, y = float(lb["x"]), float(lb["y"])
            parts = _parts(lb)
            if parts:
                for p in parts:
                    if p["kind"] == "acc":
                        cp = ACC_CP.get(p.get("name"))
                        if cp is None or not ACC_FONT.exists():
                            continue
                        af = _acc_font(max(4, round(p["size"] * fs)))
                        d.text((x + p["x"] * fs, y + p["dy"] * fs), chr(cp), font=af, fill=color, anchor="ls")
                    else:
                        t = _safe_text(p.get("text", ""))
                        if t:
                            d.text((x + p["x"] * fs, y + CAP * fs), t, font=f, fill=color, anchor="ms")
            elif text:
                d.text((x, y + CAP * fs), text, font=f, fill=color, anchor="ms")
            for k in range(max(0, min(int(lb.get("dots_above") or 0), 4))):
                cy = y - CAP * fs - fs * 0.22 - k * step
                d.ellipse([x - r, cy - r, x + r, cy + r], fill=color)
            for k in range(max(0, min(int(lb.get("dots_below") or 0), 4))):
                cy = y + CAP * fs + fs * 0.22 + k * step
                d.ellipse([x - r, cy - r, x + r, cy + r], fill=color)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue(), "image/png"
    # PDF containing the image page (vector text on top of the image)
    w, h = Image.open(png).size
    scale = 72.0 / 300.0
    doc = fitz.open()
    try:
        pg = doc.new_page(width=w * scale, height=h * scale)
        pg.insert_image(pg.rect, filename=str(png))
        _draw_pdf_page(pg, lbs, scale, fs_px, color)
        return doc.tobytes(garbage=1, deflate=True), "application/pdf"
    finally:
        doc.close()
