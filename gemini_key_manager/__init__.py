"""Standalone Gemini API key manager: rotation, health tracking, failover.

Free keys are tried first (round-robin); paid keys are fallback only, used
when every free key is exhausted, failing, or disabled.

Health model per key:
- consecutive_failures: reset on any success.
- last_used_at: round-robin rotation — the least-recently-used eligible key
  is picked first (free tier first, then paid tier).
- 429 / quota errors: exponential backoff via quota_exhausted_until
  (5 min, 15 min, 1 h, 4 h). The key is skipped until the window passes,
  then retried automatically.
- 402 / payment errors: needs_attention = 1 (surface in your admin UI);
  the key is skipped until re-enabled.
- Other errors (5xx, network, timeout): counted, 2-minute backoff applied
  so the next request immediately tries a different key instead of
  retrying the same failing key. The key becomes eligible again quickly
  if the issue was transient.

Thread-safe: an instance-level lock guards key selection and counter
updates. Network I/O happens outside the lock. Each call opens and closes
its own sqlite connection.

Secret storage: key values live in the local sqlite DB. The admin-facing
helpers only ever return masked values ("...abcd"). Never log full key
values.

Stdlib only: sqlite3, hashlib, json, threading, urllib, datetime.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

__all__ = ["KeyManager", "mask_key"]

__version__ = "1.0.0"


def mask_key(key_value: str) -> str:
    """Mask a key for display: '...abcd'. Never expose full values."""
    v = key_value or ""
    return ("..." + v[-4:]) if len(v) >= 4 else "..."


def default_safety_settings() -> list[dict]:
    """Default Gemini safety settings: sexually explicit unblocked,
    everything else at medium-and-above. Use with
    ensure_safety_settings() to inject into a request payload when the
    caller didn't specify its own safetySettings."""
    return [
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
         "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HARASSMENT",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    ]


def ensure_safety_settings(payload: dict) -> dict:
    """Inject default_safety_settings() into a generateContent payload when
    the caller didn't specify safetySettings. Explicit caller overrides are
    always respected. Returns the payload for chaining."""
    if isinstance(payload, dict) and "safetySettings" not in payload:
        payload["safetySettings"] = default_safety_settings()
    return payload


# Backoff ladder for quota/429 failures, indexed by consecutive_failures.
_BACKOFF_MINUTES = (5, 15, 60, 240)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS gemini_keys (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash             TEXT NOT NULL UNIQUE,
    key_value            TEXT NOT NULL,
    label                TEXT NOT NULL DEFAULT '',
    is_paid              INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at      TEXT,
    last_success_at      TEXT,
    total_requests       INTEGER NOT NULL DEFAULT 0,
    total_failures       INTEGER NOT NULL DEFAULT 0,
    quota_exhausted_until TEXT,
    needs_attention      INTEGER NOT NULL DEFAULT 0,
    last_used_at         TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS gemini_key_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    key_id     INTEGER,
    source     TEXT NOT NULL DEFAULT 'unknown',
    success    INTEGER NOT NULL DEFAULT 0
)
"""

_TEST_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "gemini-3.1-flash-lite:generateContent")


class KeyManager:
    """Rotating pool of Gemini API keys backed by sqlite.

    Usage:
        km = KeyManager(db_path="keys.db")
        km.add_key("AIza...", label="Free 1", is_free=True)
        result = km.post_json(url, payload, source="my-app")
    """

    def __init__(self, db_path: str = "keys.db",
                 legacy_key: str | None = None) -> None:
        """db_path: sqlite file for keys + health + usage log.

        legacy_key: optional existing key adopted as the initial paid key
        (label 'legacy-env') on first run. Falls back to the
        GEMINI_API_KEY environment variable when not provided.
        """
        self.db_path = db_path
        self.legacy_key = (legacy_key if legacy_key is not None
                           else os.environ.get("GEMINI_API_KEY", ""))
        self._lock = threading.Lock()

    # -- internals --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_str() -> str:
        # Microsecond precision so back-to-back calls within the same
        # second still rotate correctly (ORDER BY last_used_at ASC).
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    @staticmethod
    def _key_hash(key_value: str) -> str:
        return hashlib.sha256(key_value.encode("utf-8")).hexdigest()

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_USAGE_TABLE)
        # Migration: add last_used_at to pre-existing tables
        cols = [r["name"] for r in
                conn.execute("PRAGMA table_info(gemini_keys)").fetchall()]
        if "last_used_at" not in cols:
            conn.execute("ALTER TABLE gemini_keys ADD COLUMN last_used_at TEXT")
            conn.commit()

    def _log_usage(self, conn: sqlite3.Connection, key_id: int | None,
                   source: str, success: bool) -> None:
        """Lightweight per-request log: which app used which key, and
        whether it worked. Powers usage_by_source()/get_usage()."""
        try:
            conn.execute(
                "INSERT INTO gemini_key_usage (key_id, source, success)"
                " VALUES (?, ?, ?)",
                (key_id, (source or "unknown")[:64], 1 if success else 0),
            )
            # Keep the table small: retain the most recent 5000 rows.
            conn.execute(
                "DELETE FROM gemini_key_usage WHERE id NOT IN"
                " (SELECT id FROM gemini_key_usage ORDER BY id DESC LIMIT 5000)"
            )
            conn.commit()
        except Exception:
            pass  # usage logging must never break the request path

    def _log_usage_conn(self, key_id: int | None, source: str,
                        success: bool) -> None:
        """Open a short-lived connection just for usage logging."""
        try:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                self._log_usage(conn, key_id, source, success)
            finally:
                conn.close()
        except Exception:
            pass

    def _seed_legacy_key(self, conn: sqlite3.Connection) -> None:
        """First-run migration: adopt the legacy key as the initial paid key
        (label 'legacy-env') so existing setups keep working."""
        row = conn.execute("SELECT COUNT(*) AS n FROM gemini_keys").fetchone()
        if row["n"]:
            return
        legacy = (self.legacy_key or "").strip()
        if not legacy:
            return
        conn.execute(
            "INSERT INTO gemini_keys (key_hash, key_value, label, is_paid)"
            " VALUES (?, ?, 'legacy-env', 1)",
            (self._key_hash(legacy), legacy),
        )
        conn.commit()

    def _eligible_rows(self, conn: sqlite3.Connection):
        """Active keys whose backoff window has passed and which don't need
        attention, free keys first, least-recently-used first."""
        self._ensure_table(conn)
        self._seed_legacy_key(conn)
        return conn.execute(
            """SELECT id, key_value, is_paid, consecutive_failures
               FROM gemini_keys
               WHERE is_active = 1
                 AND needs_attention = 0
                 AND (quota_exhausted_until IS NULL
                      OR quota_exhausted_until <= datetime('now'))
               ORDER BY is_paid ASC,
                        last_used_at ASC,
                        consecutive_failures ASC,
                        id ASC"""
        ).fetchall()

    def _all_active_keys(self) -> list[tuple[int, str]]:
        """All active keys ignoring backoff windows, free first.
        Last-resort pool when every key is cooling down."""
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                rows = conn.execute(
                    """SELECT id, key_value
                       FROM gemini_keys
                       WHERE is_active = 1 AND needs_attention = 0
                       ORDER BY is_paid ASC, consecutive_failures ASC, id ASC"""
                ).fetchall()
                return [(r["id"], r["key_value"]) for r in rows]
            finally:
                conn.close()

    @staticmethod
    def _classify_http_error(code: int | None) -> str:
        if code == 429:
            return "quota"
        if code == 402:
            return "payment"
        return "other"

    def _row_to_public(self, row) -> dict:
        exhausted = bool(row["quota_exhausted_until"])
        if exhausted:
            try:
                raw = row["quota_exhausted_until"]
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                    try:
                        until = datetime.strptime(
                            raw, fmt).replace(tzinfo=timezone.utc)
                        break
                    except (ValueError, TypeError):
                        until = None
                if until is None:
                    raise ValueError("bad timestamp")
                exhausted = until > datetime.now(timezone.utc)
            except (ValueError, TypeError):
                exhausted = True
        if row["needs_attention"]:
            status = "needs_attention"
        elif not row["is_active"]:
            status = "disabled"
        elif exhausted:
            status = "cooling_down"
        elif row["consecutive_failures"]:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "id": row["id"],
            "label": row["label"],
            "key_masked": mask_key(row["key_value"]),
            "is_paid": bool(row["is_paid"]),
            "is_active": bool(row["is_active"]),
            "status": status,
            "consecutive_failures": row["consecutive_failures"],
            "total_requests": row["total_requests"],
            "total_failures": row["total_failures"],
            "last_success_at": row["last_success_at"],
            "last_failure_at": row["last_failure_at"],
            "quota_exhausted_until": row["quota_exhausted_until"],
            "needs_attention": bool(row["needs_attention"]),
            "created_at": row["created_at"],
        }

    # -- selection & reporting ---------------------------------------------

    def get_candidate_keys(self) -> list[tuple[int, str]]:
        """Ordered (key_id, key_value) candidates. Empty when nothing usable."""
        with self._lock:
            conn = self._connect()
            try:
                rows = self._eligible_rows(conn)
                return [(r["id"], r["key_value"]) for r in rows]
            finally:
                conn.close()

    def get_key(self, source: str = "unknown") -> tuple[int, str] | None:
        """Next key in round-robin rotation, or None when the pool is
        exhausted.

        Returns the least-recently-used eligible key (free tier first) and
        marks it used so the next call rotates to a different key. This
        distributes load across all keys instead of hammering the first one.
        """
        cands = self.get_candidate_keys()
        if not cands:
            return None
        key_id, key_value = cands[0]
        # Mark as used so rotation advances even if the caller doesn't use
        # post_json.
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                conn.execute(
                    "UPDATE gemini_keys SET last_used_at = ? WHERE id = ?",
                    (self._now_str(), key_id),
                )
                conn.commit()
            finally:
                conn.close()
        return (key_id, key_value)

    def report_success(self, key_id: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = 0,
                           last_success_at = ?,
                           last_used_at = ?,
                           total_requests = total_requests + 1,
                           quota_exhausted_until = NULL
                       WHERE id = ?""",
                    (self._now_str(), self._now_str(), key_id),
                )
                conn.commit()
            finally:
                conn.close()

    def report_failure(self, key_id: int, error_type: str) -> None:
        """error_type: 'quota' (429), 'payment' (402), or 'other'."""
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                row = conn.execute(
                    "SELECT consecutive_failures FROM gemini_keys WHERE id = ?",
                    (key_id,),
                ).fetchone()
                if not row:
                    return
                failures = (row["consecutive_failures"] or 0) + 1
                if error_type == "quota":
                    wait = _BACKOFF_MINUTES[min(failures - 1,
                                               len(_BACKOFF_MINUTES) - 1)]
                    until = (datetime.now(timezone.utc)
                             + timedelta(minutes=wait)).strftime(
                                 "%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """UPDATE gemini_keys
                           SET consecutive_failures = ?,
                               last_failure_at = ?,
                               total_requests = total_requests + 1,
                               total_failures = total_failures + 1,
                               quota_exhausted_until = ?
                           WHERE id = ?""",
                        (failures, self._now_str(), until, key_id),
                    )
                elif error_type == "payment":
                    conn.execute(
                        """UPDATE gemini_keys
                           SET consecutive_failures = ?,
                               last_failure_at = ?,
                               total_requests = total_requests + 1,
                               total_failures = total_failures + 1,
                               needs_attention = 1
                           WHERE id = ?""",
                        (failures, self._now_str(), key_id),
                    )
                else:
                    # "Other" errors (5xx, timeout, network): short 2-minute
                    # backoff so the next request immediately tries a
                    # different key instead of retrying this same failing key.
                    until = (datetime.now(timezone.utc)
                             + timedelta(minutes=2)).strftime(
                                 "%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """UPDATE gemini_keys
                           SET consecutive_failures = ?,
                               last_failure_at = ?,
                               total_requests = total_requests + 1,
                               total_failures = total_failures + 1,
                               quota_exhausted_until = ?
                           WHERE id = ?""",
                        (failures, self._now_str(), until, key_id),
                    )
                conn.commit()
            finally:
                conn.close()

    def retry_exhausted(self) -> int:
        """Clear backoff windows that have passed so those keys become
        eligible again. Returns the number of keys reopened. (Selection
        already skips keys still inside their window, so this is
        bookkeeping.)"""
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                cur = conn.execute(
                    """UPDATE gemini_keys
                       SET quota_exhausted_until = NULL
                       WHERE quota_exhausted_until IS NOT NULL
                         AND quota_exhausted_until <= datetime('now')"""
                )
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    # -- request path ------------------------------------------------------

    def post_json(self, url: str, payload: dict, timeout: int = 30,
                  source: str = "unknown") -> dict | None:
        """POST JSON to a Gemini endpoint with transparent key rotation.

        Tries each eligible key in order (free first, least-recently-used
        first). On a failure the key's health is recorded (with backoff so
        the next request skips it) and the next key is tried immediately. If
        every eligible key is cooling down, falls back to trying all active
        keys ignoring backoff as a last resort. Returns the parsed JSON
        response, or None when every key failed / no keys exist.

        `source` names the calling app (e.g. "my-app"); each attempt is
        logged for usage_by_source()/get_usage().
        """
        candidates = self.get_candidate_keys()
        if not candidates:
            # Last resort: all keys are cooling down — try them anyway
            # ignoring backoff rather than failing the request outright.
            candidates = self._all_active_keys()
        for key_id, key_value in candidates:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": key_value},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                self.report_failure(key_id, self._classify_http_error(e.code))
                self._log_usage_conn(key_id, source, False)
                continue
            except Exception:
                self.report_failure(key_id, "other")
                self._log_usage_conn(key_id, source, False)
                continue
            self.report_success(key_id)
            self._log_usage_conn(key_id, source, True)
            return data
        return None

    # -- admin helpers (key values are masked) -------------------------------

    def list_keys(self) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                self._seed_legacy_key(conn)
                rows = conn.execute(
                    "SELECT * FROM gemini_keys ORDER BY is_paid ASC, id ASC"
                ).fetchall()
                return [self._row_to_public(r) for r in rows]
            finally:
                conn.close()

    def add_key(self, key_value: str, label: str = "",
                is_paid: bool = False,
                is_free: bool | None = None) -> dict:
        """Add a key. is_free=True is shorthand for is_paid=False."""
        key_value = (key_value or "").strip()
        if not key_value:
            raise ValueError("key is required")
        if is_free is not None:
            is_paid = not is_free
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                self._seed_legacy_key(conn)
                exists = conn.execute(
                    "SELECT id FROM gemini_keys WHERE key_hash = ?",
                    (self._key_hash(key_value),),
                ).fetchone()
                if exists:
                    raise ValueError("key already exists")
                cur = conn.execute(
                    """INSERT INTO gemini_keys (key_hash, key_value, label, is_paid)
                       VALUES (?, ?, ?, ?)""",
                    (self._key_hash(key_value), key_value,
                     (label or "").strip(), 1 if is_paid else 0),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM gemini_keys WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
                return self._row_to_public(row)
            finally:
                conn.close()

    def delete_key(self, key_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                cur = conn.execute(
                    "DELETE FROM gemini_keys WHERE id = ?", (key_id,))
                conn.commit()
                return (cur.rowcount or 0) > 0
            finally:
                conn.close()

    def set_active(self, key_id: int, is_active: bool) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                # Re-enabling also clears the attention flag and backoff so
                # the key is tried again right away.
                conn.execute(
                    """UPDATE gemini_keys
                       SET is_active = ?,
                           needs_attention = CASE WHEN ? = 1 THEN 0
                                               ELSE needs_attention END,
                           quota_exhausted_until = CASE WHEN ? = 1 THEN NULL
                                                   ELSE quota_exhausted_until END
                       WHERE id = ?""",
                    (1 if is_active else 0, 1 if is_active else 0,
                     1 if is_active else 0, key_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM gemini_keys WHERE id = ?", (key_id,)
                ).fetchone()
                return self._row_to_public(row) if row else None
            finally:
                conn.close()

    def test_key(self, key_id: int) -> dict | None:
        """Minimal live call against one key. Returns health, never the key."""
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                row = conn.execute(
                    "SELECT * FROM gemini_keys WHERE id = ?", (key_id,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        payload = {
            "contents": [{"role": "user",
                          "parts": [{"text": "Reply with the single word: ok"}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 5},
        }
        req = urllib.request.Request(
            _TEST_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": row["key_value"]},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if p.get("text"))
            self.report_success(key_id)
            return {"ok": True, "status": "healthy",
                    "sample": text.strip()[:40]}
        except urllib.error.HTTPError as e:
            error_type = self._classify_http_error(e.code)
            self.report_failure(key_id, error_type)
            return {"ok": False, "status": error_type, "http_code": e.code}
        except Exception as e:
            self.report_failure(key_id, "other")
            return {"ok": False, "status": "other",
                    "error": str(e)[:120]}

    # -- usage analytics -----------------------------------------------------

    def get_usage(self, limit: int = 100) -> list[dict]:
        """Recent key usage: last N requests with source, key label,
        timestamp, and success/failure."""
        limit = max(1, min(int(limit or 100), 500))
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                rows = conn.execute(
                    """SELECT u.id, u.created_at, u.key_id, u.source, u.success,
                              k.label AS key_label, k.is_paid AS is_paid
                       FROM gemini_key_usage u
                       LEFT JOIN gemini_keys k ON k.id = u.key_id
                       ORDER BY u.id DESC
                       LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [{
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "key_id": r["key_id"],
                    "key_label": r["key_label"],
                    "is_paid": bool(r["is_paid"]) if r["is_paid"] is not None
                    else None,
                    "source": r["source"],
                    "success": bool(r["success"]),
                } for r in rows]
            finally:
                conn.close()

    def usage_by_source(self, limit: int = 100) -> list[dict]:
        """Aggregate request/success/failure counts per source over the
        recent usage window."""
        limit = max(1, min(int(limit or 100), 500))
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_table(conn)
                rows = conn.execute(
                    """SELECT source,
                              COUNT(*) AS requests,
                              SUM(success) AS successes,
                              SUM(1 - success) AS failures
                       FROM (SELECT source, success FROM gemini_key_usage
                             ORDER BY id DESC LIMIT ?)
                       GROUP BY source
                       ORDER BY requests DESC""",
                    (limit,),
                ).fetchall()
                return [{
                    "source": r["source"],
                    "requests": r["requests"],
                    "successes": r["successes"] or 0,
                    "failures": r["failures"] or 0,
                } for r in rows]
            finally:
                conn.close()
