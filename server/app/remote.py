"""Dispatcher -> Cloud Run page worker client (stdlib only: the container image has no httpx)."""
from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.request
from pathlib import Path

from .config import Settings

log = logging.getLogger("spm.remote")


class RemoteError(Exception):
    """retryable=True: network error / timeout / 5xx.  fatal=True: the engine itself rejected the page
    (deterministic, local recognition would fail the same way) -> fail the job, no local fallback."""

    def __init__(self, msg: str, retryable: bool = False, fatal: bool = False):
        super().__init__(msg)
        self.retryable, self.fatal = retryable, fatal


def _multipart(fields: dict[str, str], file_name: str, data: bytes) -> tuple[bytes, str]:
    b = "----spm" + secrets.token_hex(12)
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                 f"Content-Type: image/png\r\n\r\n".encode("utf-8"))
    parts.append(data)
    parts.append(f"\r\n--{b}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def recognize_page(s: Settings, png: Path, engine: str, index: int, pg: dict | None) -> dict:
    """POST one page PNG to the worker; returns the worker's per-page result dict (merge_page shape)."""
    fields = {"engine": engine, "index": str(index)}
    if pg:
        fields["pt_w"], fields["pt_h"] = repr(float(pg["pt_w"])), repr(float(pg["pt_h"]))
    body, ctype = _multipart(fields, "page.png", png.read_bytes())
    req = urllib.request.Request(s.remote_worker_url + "/api/page", data=body, method="POST", headers={
        "Content-Type": ctype, "X-SPM-Worker-Key": s.worker_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=s.remote_timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = str(json.loads(e.read().decode("utf-8")).get("error", ""))[:300]
        except Exception:  # noqa: BLE001
            pass
        if e.code == 422 and detail.startswith("engine_"):
            raise RemoteError(detail, fatal=True)
        raise RemoteError(f"worker http {e.code} {detail}", retryable=e.code >= 500 or e.code == 429)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise RemoteError(f"worker unreachable: {type(e).__name__}", retryable=True)
