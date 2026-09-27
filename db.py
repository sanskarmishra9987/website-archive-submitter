"""
Database layer for the Website Archive Submitter.
Uses plain SQLite (stdlib) so there's zero setup friction - just run the app.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "archive_repository.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT UNIQUE NOT NULL,
    added_at REAL NOT NULL,
    last_scan_at REAL,
    last_submission_at REAL,
    status TEXT NOT NULL DEFAULT 'idle'   -- idle | discovering | queued | submitting | done
);

CREATE TABLE IF NOT EXISTS urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    original_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    discovery_source TEXT,              -- html_link | sitemap | robots_txt | canonical
    discovery_timestamp REAL NOT NULL,
    http_status INTEGER,
    UNIQUE(domain_id, normalized_url)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id INTEGER NOT NULL REFERENCES urls(id),
    service TEXT NOT NULL,              -- wayback | archive_today
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | success | failed
    submission_timestamp REAL,
    archive_url TEXT,
    archive_identifier TEXT,
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempted_time REAL
);

CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain_id);
CREATE INDEX IF NOT EXISTS idx_sub_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_sub_url ON submissions(url_id);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Crash recovery: anything left mid-flight from a previous run
        # goes back to pending so the worker picks it up again.
        conn.execute(
            "UPDATE submissions SET status='pending' WHERE status='processing'"
        )
        conn.execute(
            "UPDATE domains SET status='idle' WHERE status IN ('discovering','submitting')"
        )


def get_or_create_domain(domain: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM domains WHERE domain=?", (domain,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO domains (domain, added_at, status) VALUES (?,?,?)",
            (domain, time.time(), "idle"),
        )
        return cur.lastrowid


def insert_url_if_new(domain_id: int, original_url: str, normalized_url: str, source: str, http_status=None):
    """Returns True if this was a brand-new URL (not seen before for this domain)."""
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO urls (domain_id, original_url, normalized_url, discovery_source,
                   discovery_timestamp, http_status) VALUES (?,?,?,?,?,?)""",
                (domain_id, original_url, normalized_url, source, time.time(), http_status),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # already known -> incremental backup skips it


def queue_submission(url_id: int, service: str = "wayback"):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM submissions WHERE url_id=? AND service=? AND status IN ('pending','processing','success')",
            (url_id, service),
        ).fetchone()
        if existing:
            return  # don't re-queue something already archived/queued
        conn.execute(
            "INSERT INTO submissions (url_id, service, status) VALUES (?,?, 'pending')",
            (url_id, service),
        )


def force_requeue(url_id: int, service: str = "wayback"):
    """User explicitly asked for a fresh snapshot even if one already exists."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO submissions (url_id, service, status) VALUES (?,?, 'pending')",
            (url_id, service),
        )


def next_pending_submission():
    with get_conn() as conn:
        row = conn.execute(
            """SELECT s.id as sub_id, u.id as url_id, u.normalized_url, s.service
               FROM submissions s JOIN urls u ON u.id = u.id AND u.id = s.url_id
               WHERE s.status='pending' ORDER BY s.id LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE submissions SET status='processing', last_attempted_time=? WHERE id=?",
            (time.time(), row["sub_id"]),
        )
        return dict(row)


def mark_submission_result(sub_id: int, success: bool, archive_url=None, archive_identifier=None, error=None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE submissions SET status=?, submission_timestamp=?, archive_url=?,
               archive_identifier=?, error_message=?, attempts=attempts+1, last_attempted_time=?
               WHERE id=?""",
            (
                "success" if success else "failed",
                time.time() if success else None,
                archive_url,
                archive_identifier,
                error,
                time.time(),
                sub_id,
            ),
        )


def retry_failed(max_attempts=3):
    """Requeue failed submissions that haven't exhausted their retry budget."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE submissions SET status='pending' WHERE status='failed' AND attempts < ?",
            (max_attempts,),
        )


def get_stats():
    with get_conn() as conn:
        domains = conn.execute("SELECT COUNT(*) c FROM domains").fetchone()["c"]
        total_urls = conn.execute("SELECT COUNT(*) c FROM urls").fetchone()["c"]
        queued = conn.execute("SELECT COUNT(*) c FROM submissions WHERE status='pending'").fetchone()["c"]
        processing = conn.execute("SELECT COUNT(*) c FROM submissions WHERE status='processing'").fetchone()["c"]
        success = conn.execute("SELECT COUNT(*) c FROM submissions WHERE status='success'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) c FROM submissions WHERE status='failed'").fetchone()["c"]
        return {
            "domains": domains,
            "total_urls": total_urls,
            "queued": queued,
            "processing": processing,
            "success": success,
            "failed": failed,
        }


def list_domains():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM domains ORDER BY id DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            total = conn.execute("SELECT COUNT(*) c FROM urls WHERE domain_id=?", (d["id"],)).fetchone()["c"]
            succ = conn.execute(
                """SELECT COUNT(*) c FROM submissions s JOIN urls u ON s.url_id=u.id
                   WHERE u.domain_id=? AND s.status='success'""", (d["id"],)
            ).fetchone()["c"]
            fail = conn.execute(
                """SELECT COUNT(*) c FROM submissions s JOIN urls u ON s.url_id=u.id
                   WHERE u.domain_id=? AND s.status='failed'""", (d["id"],)
            ).fetchone()["c"]
            pend = conn.execute(
                """SELECT COUNT(*) c FROM submissions s JOIN urls u ON s.url_id=u.id
                   WHERE u.domain_id=? AND s.status IN ('pending','processing')""", (d["id"],)
            ).fetchone()["c"]
            d.update(total_urls=total, success=succ, failed=fail, pending=pend)
            result.append(d)
        return result


def set_domain_status(domain_id: int, status: str, touch_scan=False, touch_submit=False):
    with get_conn() as conn:
        if touch_scan:
            conn.execute("UPDATE domains SET status=?, last_scan_at=? WHERE id=?", (status, time.time(), domain_id))
        elif touch_submit:
            conn.execute("UPDATE domains SET status=?, last_submission_at=? WHERE id=?", (status, time.time(), domain_id))
        else:
            conn.execute("UPDATE domains SET status=? WHERE id=?", (status, domain_id))


def list_urls_for_domain(domain_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT u.*,
                      (SELECT s.status FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) as latest_status,
                      (SELECT s.archive_url FROM submissions s WHERE s.url_id=u.id AND s.status='success' ORDER BY s.id DESC LIMIT 1) as archive_url,
                      (SELECT s.error_message FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) as error_message
               FROM urls u WHERE u.domain_id=? ORDER BY u.id""",
            (domain_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def search_repository(query: str, service: str = None, status: str = None):
    with get_conn() as conn:
        sql = """SELECT u.*, d.domain,
                         (SELECT s.status FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) as latest_status,
                         (SELECT s.service FROM submissions s WHERE s.url_id=u.id ORDER BY s.id DESC LIMIT 1) as latest_service,
                         (SELECT s.archive_url FROM submissions s WHERE s.url_id=u.id AND s.status='success' ORDER BY s.id DESC LIMIT 1) as archive_url
                  FROM urls u JOIN domains d ON d.id=u.domain_id
                  WHERE (u.normalized_url LIKE ? OR d.domain LIKE ?)"""
        params = [f"%{query}%", f"%{query}%"]
        if status:
            sql += """ AND u.id IN (SELECT url_id FROM submissions WHERE status=?)"""
            params.append(status)
        if service:
            sql += """ AND u.id IN (SELECT url_id FROM submissions WHERE service=?)"""
            params.append(service)
        sql += " ORDER BY u.id DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def submission_history(url_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE url_id=? ORDER BY id", (url_id,)
        ).fetchall()
        return [dict(r) for r in rows]
