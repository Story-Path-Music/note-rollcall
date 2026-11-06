"""SQLite store: users, usage (quota), nonces, jobs."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, quota_limit INTEGER NOT NULL,
  dev INTEGER NOT NULL DEFAULT 0, last_login TEXT);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, date TEXT NOT NULL,
  file_sha256 TEXT NOT NULL, job_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS usage_user_date ON usage(user_id, date);
CREATE TABLE IF NOT EXISTS remote_usage(
  month TEXT PRIMARY KEY, vcpu_seconds REAL NOT NULL DEFAULT 0, gib_seconds REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY, exp REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL, page INTEGER DEFAULT 0,
  pages INTEGER DEFAULT 0, error TEXT, engine TEXT, source_type TEXT, filename TEXT,
  created_at TEXT NOT NULL);
"""

# usage.status: reserved (counts) -> done (counts) | failed (does not count)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()

    # ---- users ----
    def upsert_user(self, uid: int, name: str, quota: int, dev: bool = False):
        with self.conn() as c:
            c.execute(
                "INSERT INTO users(id,name,quota_limit,dev,last_login) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, quota_limit=excluded.quota_limit,"
                " dev=excluded.dev, last_login=excluded.last_login",
                (uid, name, quota, int(dev), clock.now().isoformat()),
            )

    def get_user(self, uid: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return dict(r) if r else None

    # ---- nonces ----
    def use_nonce(self, nonce: str, exp: float) -> bool:
        """True if fresh (now consumed); False if replayed."""
        now = clock.now().timestamp()
        with self.conn() as c:
            c.execute("DELETE FROM nonces WHERE exp < ?", (now - 60,))
            try:
                c.execute("INSERT INTO nonces(nonce,exp) VALUES(?,?)", (nonce, exp))
            except sqlite3.IntegrityError:
                return False
        return True

    # ---- quota ----
    def used_today(self, uid: int, c=None) -> int:
        q = "SELECT COUNT(*) FROM usage WHERE user_id=? AND date=? AND status IN ('reserved','done')"
        args = (uid, clock.today_taipei())
        if c is not None:
            return c.execute(q, args).fetchone()[0]
        with self.conn() as cc:
            return cc.execute(q, args).fetchone()[0]

    def quota_info(self, uid: int) -> dict:
        u = self.get_user(uid)
        limit = u["quota_limit"] if u else 0
        used = self.used_today(uid)
        return {"limit": limit, "used": used, "remaining": max(0, limit - used), "date": clock.today_taipei()}

    def reserve(self, uid: int, sha: str, new_job_id: str):
        """Atomically decide: ('cached', job_id) | ('reserved', new_job_id) | ('exceeded', None)."""
        today = clock.today_taipei()
        with self.lock, self.conn() as c:
            row = c.execute(
                "SELECT job_id, status FROM usage WHERE user_id=? AND date=? AND file_sha256=? "
                "AND status IN ('reserved','done') ORDER BY id DESC LIMIT 1",
                (uid, today, sha),
            ).fetchone()
            if row:
                j = c.execute("SELECT status FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
                if j and j["status"] != "error":
                    return "cached", row["job_id"]
                # job record gone (retention cleanup): re-run under the existing charge
                c.execute("UPDATE usage SET job_id=?, status='reserved' WHERE job_id=?", (new_job_id, row["job_id"]))
                return "reserved", new_job_id
            u = c.execute("SELECT quota_limit FROM users WHERE id=?", (uid,)).fetchone()
            limit = u["quota_limit"] if u else 0
            if self.used_today(uid, c) >= limit:
                return "exceeded", None
            c.execute(
                "INSERT INTO usage(user_id,date,file_sha256,job_id,status,created_at) VALUES(?,?,?,?,?,?)",
                (uid, today, sha, new_job_id, "reserved", clock.now().isoformat()),
            )
            return "reserved", new_job_id

    def finish_usage(self, job_id: str, ok: bool):
        with self.conn() as c:
            c.execute("UPDATE usage SET status=? WHERE job_id=?", ("done" if ok else "failed", job_id))

    # ---- jobs ----
    def create_job(self, job_id, uid, engine, source_type, filename, pages):
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO jobs(job_id,user_id,status,page,pages,error,engine,source_type,filename,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, uid, "queued", 0, pages, None, engine, source_type, filename, clock.now().isoformat()),
            )

    def update_job(self, job_id, **kw):
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", (*kw.values(), job_id))

    def get_job(self, job_id: str):
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(r) if r else None

    def queue_position(self, job_id: str) -> int:
        """Jobs ahead of this one: running jobs plus queued jobs created earlier."""
        with self.conn() as c:
            r = c.execute("SELECT created_at, rowid, status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not r or r[2] != "queued":
                return 0
            return c.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='running' OR (status='queued' AND "
                "(created_at < ? OR (created_at = ? AND rowid < ?)))", (r[0], r[0], r[1])).fetchone()[0]

    def pages_ahead(self, job_id: str) -> int:
        """Total pages of the jobs counted by queue_position (for a rough wait estimate)."""
        with self.conn() as c:
            r = c.execute("SELECT created_at, rowid, status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not r or r[2] != "queued":
                return 0
            return c.execute(
                "SELECT COALESCE(SUM(pages),0) FROM jobs WHERE status='running' OR (status='queued' AND "
                "(created_at < ? OR (created_at = ? AND rowid < ?)))", (r[0], r[0], r[1])).fetchone()[0]

    def interrupted_jobs(self):
        with self.conn() as c:
            return [r["job_id"] for r in c.execute("SELECT job_id FROM jobs WHERE status IN ('queued','running')")]

    def old_jobs(self, before_iso: str):
        with self.conn() as c:
            return [r["job_id"] for r in c.execute("SELECT job_id FROM jobs WHERE created_at < ?", (before_iso,))]

    def delete_job(self, job_id: str):
        with self.conn() as c:
            c.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))

    # ---- admin ----
    # ---- Cloud Run monthly usage (billing month, Pacific time) ----
    def remote_usage(self, month: str) -> tuple[float, float]:
        with self.conn() as c:
            r = c.execute("SELECT vcpu_seconds, gib_seconds FROM remote_usage WHERE month=?", (month,)).fetchone()
            return (r[0], r[1]) if r else (0.0, 0.0)

    def remote_add(self, month: str, dv: float, dg: float, cap: tuple[float, float] | None = None) -> bool:
        """Atomically add (dv, dg) to the month.  With cap=(max_v, max_g): only if the result stays within both
        caps (a reservation); returns False and changes nothing otherwise.  Without cap: unconditional (settle),
        clamped at 0."""
        with self.lock, self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT OR IGNORE INTO remote_usage(month) VALUES(?)", (month,))
            v, g = c.execute("SELECT vcpu_seconds, gib_seconds FROM remote_usage WHERE month=?", (month,)).fetchone()
            if cap is not None and (v + dv > cap[0] or g + dg > cap[1]):
                return False
            c.execute("UPDATE remote_usage SET vcpu_seconds=?, gib_seconds=? WHERE month=?",
                      (max(0.0, v + dv), max(0.0, g + dg), month))
            return True

    def usage_report(self) -> list[dict]:
        today = clock.today_taipei()
        with self.conn() as c:
            rows = c.execute(
                "SELECT u.id, u.name, u.quota_limit, "
                "SUM(CASE WHEN g.status IN ('reserved','done') THEN 1 ELSE 0 END) AS used, "
                "SUM(CASE WHEN g.status='failed' THEN 1 ELSE 0 END) AS failed "
                "FROM usage g JOIN users u ON u.id=g.user_id WHERE g.date=? GROUP BY u.id ORDER BY used DESC",
                (today,),
            ).fetchall()
            return [dict(r) for r in rows]
