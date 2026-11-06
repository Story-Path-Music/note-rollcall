"""OMR job progress: engine stderr markers -> overall monotonic fraction, stage, ETA.

Marker contract (docs/CONTRACT.md section 1): an engine writes lines `@@progress {"stage":"detect","p":0.4}` to
stderr; p = 0..1 progress *within* that stage.  Everything else on stderr is ordinary log text.
"""
from __future__ import annotations

import json
import threading
import time

MARKER = "@@progress "
# page pipeline order and share of one page's wall time (measured with homr on CPU; see CONTRACT)
STAGES = ("render", "detect", "transcribe", "post")
STAGE_WEIGHTS = {"render": 0.03, "detect": 0.50, "transcribe": 0.38, "post": 0.09}
PUBLIC_STAGES = ("queued",) + STAGES + ("done",)
MAX_RUNNING_FRACTION = 0.99  # 1.0 only when the job is really done


def parse_marker(line: str):
    """'@@progress {...}' -> (stage, p) or None.  Unknown stage / bad JSON / bad p -> None."""
    line = line.strip()
    if not line.startswith(MARKER):
        return None
    try:
        d = json.loads(line[len(MARKER):])
        stage, p = d["stage"], float(d["p"])
    except (ValueError, KeyError, TypeError):
        return None
    if p != p:  # NaN
        return None
    if stage == "done":  # engine finished this page (the job's own "done" is set by the job manager)
        stage, p = "post", 1.0
    if stage not in STAGES:
        return None
    return stage, min(1.0, max(0.0, p))


def page_fraction(stage: str, p: float) -> float:
    """Position inside one page, 0..1."""
    done = 0.0
    for s in STAGES:
        if s == stage:
            return done + STAGE_WEIGHTS[s] * p
        done += STAGE_WEIGHTS[s]
    return 0.0


class JobProgress:
    """Thread-safe tracker for one running job."""

    def __init__(self, pages: int):
        self.pages = max(1, pages)
        self.page = 1
        self.stage = "render"
        self.fraction = 0.0
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def begin_page(self, page: int):  # 1-based
        with self._lock:
            self.page = page
            self.stage = "render"
            self._bump(0.0)

    def remote_progress(self, rendered: int, done: int):
        """Remote (parallel) path: pages are rendered one after another while workers recognise them concurrently.
        Overall fraction = render share 0.03 + recognition share 0.96 by pages completed; monotonic, < 0.99."""
        with self._lock:
            self.page = min(self.pages, done + 1)
            self.stage = "render" if rendered < self.pages and done == 0 else "transcribe"
            f = 0.03 * rendered / self.pages + 0.96 * done / self.pages
            self.fraction = max(self.fraction, min(MAX_RUNNING_FRACTION, f))

    def mark(self, stage: str, p: float):
        with self._lock:
            self.stage = stage
            self._bump(page_fraction(stage, p))

    def _bump(self, within_page: float):
        f = ((self.page - 1) + within_page) / self.pages
        self.fraction = max(self.fraction, min(MAX_RUNNING_FRACTION, f))  # monotonic non-decreasing

    def snapshot(self):
        with self._lock:
            return self.page, self.stage, self.fraction, time.monotonic() - self.started


def eta_seconds(fraction: float, elapsed: float, pages: int, est_per_page: int):
    """Elapsed-based once enough is done to extrapolate, otherwise the configured per-page estimate."""
    if fraction >= 1.0:
        return 0
    if fraction >= 0.1 and elapsed > 0:
        return max(0, int(round(elapsed * (1.0 - fraction) / fraction)))
    if est_per_page > 0:
        return max(0, int(round(est_per_page * pages * (1.0 - fraction))))
    return None
