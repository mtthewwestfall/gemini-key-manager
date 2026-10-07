"""Gemini API key manager: free-first rotation, health tracking, instant failover.

Design:
- Free (non-paid) keys are always tried first, healthiest first.
- Paid keys are only used when every free key is exhausted, disabled, or in backoff.
- Quota failures (429) put a key into exponential backoff: 5m -> 15m -> 1h -> 4h.
- Payment failures (402) flag the key as needs_attention; it is skipped until
  an admin re-enables it.
- Other failures (5xx, network) count against the key and deprioritize it.
- Success resets the failure count and clears any backoff.
- retry_exhausted() clears backoff windows that have expired so keys are
  re-tried automatically.

"How much is left" is tracked as request/failure counts per key. Google's
free-tier quota is not exposed as a numeric remaining balance on the key,
so the health panel reports usage and failure rate per key instead.
"""

import hashlib
import sqlite3
import threading
import time

import requests

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_value TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    is_paid INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_requests INTEGER NOT NULL DEFAULT 0,
    total_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER,
    last_failure_at INTEGER,
    quota_exhausted_until INTEGER DEFAULT 0,
    needs_attention INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active, is_paid);
"""

BACKOFF_LADDER = (300, 900, 3600, 14400)  # 5m, 15m, 1h, 4h

GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _now() -> int:
    return int(time.time())


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "..." + key[-4:]
    return "..." + key[-4:]


class KeyManager:
    def __init__(self, db_path: str = "keys.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ CRUD

    def add_key(self, key: str, label: str = "", is_paid: bool = False) -> dict:
        key = key.strip()
        if not key:
            raise ValueError("key must not be empty")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        with self._lock, self._conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO api_keys (key_value, key_hash, label, is_paid, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (key, key_hash, label or key[-4:], 1 if is_paid else 0, _now()),
                )
            except sqlite3.IntegrityError:
                raise ValueError("that key is already registered")
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return self._public(dict(row))

    def delete_key(self, key_id: int) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            return cur.rowcount > 0

    def set_active(self, key_id: int, active: bool) -> bool:
        with self._lock, self._conn() as conn:
            if active:
                # Re-enabling clears attention flag and backoff so the key is
                # retried immediately.
                cur = conn.execute(
                    "UPDATE api_keys SET is_active = 1, needs_attention = 0,"
                    " quota_exhausted_until = 0, consecutive_failures = 0"
                    " WHERE id = ?",
                    (key_id,),
                )
            else:
                cur = conn.execute(
                    "UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,)
                )
            return cur.rowcount > 0

    def list_keys(self) -> list:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
        return [self._public(dict(r)) for r in rows]

    def _public(self, row: dict) -> dict:
        """Strip the raw key value; only ever return a masked suffix."""
        row = dict(row)
        row["masked"] = mask_key(row.pop("key_value"))
        row.pop("key_hash", None)
        row["is_paid"] = bool(row["is_paid"])
        row["is_active"] = bool(row["is_active"])
        row["needs_attention"] = bool(row["needs_attention"])
        row["status"] = self._status(row)
        return row

    @staticmethod
    def _status(row: dict) -> str:
        if not row["is_active"]:
            return "disabled"
        if row["needs_attention"]:
            return "needs_attention"
        if row.get("quota_exhausted_until", 0) > _now():
            return "cooling_down"
        if row.get("consecutive_failures", 0) > 0:
            return "degraded"
        return "healthy"

    # ------------------------------------------------------------- selection

    def _candidates(self, conn) -> list:
        """Eligible keys, free first then paid, healthiest first."""
        now = _now()
        rows = conn.execute(
            "SELECT * FROM api_keys WHERE is_active = 1 AND needs_attention = 0"
            " AND quota_exhausted_until <= ?"
            " ORDER BY is_paid ASC, consecutive_failures ASC,"
            " COALESCE(last_failure_at, 0) ASC, id ASC",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_candidate_keys(self) -> list:
        with self._lock, self._conn() as conn:
            return self._candidates(conn)

    # ---------------------------------------------------------------- health

    def report_success(self, key_id: int):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE api_keys SET consecutive_failures = 0,"
                " total_requests = total_requests + 1, last_success_at = ?,"
                " quota_exhausted_until = 0 WHERE id = ?",
                (_now(), key_id),
            )

    def report_failure(self, key_id: int, error_type: str):
        """error_type: quota | payment | server | other"""
        now = _now()
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if not row:
                return
            fails = row["consecutive_failures"] + 1
            if error_type == "quota":
                ladder = BACKOFF_LADDER[min(fails - 1, len(BACKOFF_LADDER) - 1)]
                conn.execute(
                    "UPDATE api_keys SET consecutive_failures = ?,"
                    " total_requests = total_requests + 1,"
                    " total_failures = total_failures + 1,"
                    " last_failure_at = ?, quota_exhausted_until = ?"
                    " WHERE id = ?",
                    (fails, now, now + ladder, key_id),
                )
            elif error_type == "payment":
                conn.execute(
                    "UPDATE api_keys SET consecutive_failures = ?,"
                    " total_requests = total_requests + 1,"
                    " total_failures = total_failures + 1,"
                    " last_failure_at = ?, needs_attention = 1 WHERE id = ?",
                    (fails, now, key_id),
                )
            else:
                conn.execute(
                    "UPDATE api_keys SET consecutive_failures = ?,"
                    " total_requests = total_requests + 1,"
                    " total_failures = total_failures + 1,"
                    " last_failure_at = ? WHERE id = ?",
                    (fails, now, key_id),
                )

    def retry_exhausted(self) -> int:
        """Clear expired backoff windows so keys are retried. Returns count."""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET quota_exhausted_until = 0"
                " WHERE quota_exhausted_until > 0 AND quota_exhausted_until <= ?",
                (_now(),),
            )
            return cur.rowcount

    # --------------------------------------------------------------- forward

    @staticmethod
    def _error_type(status_code: int, exc: Exception | None) -> str:
        if status_code == 429:
            return "quota"
        if status_code == 402:
            return "payment"
        if status_code and 500 <= status_code < 600:
            return "server"
        return "other"

    def generate(self, model: str, payload: dict, timeout: int = 60) -> dict:
        """Forward a generateContent request through the key rotation.

        Tries each eligible key in order; on failure the next key is tried
        immediately (no delay to the caller). Returns the parsed JSON body.
        Raises RuntimeError if every key failed.
        """
        with self._lock, self._conn() as conn:
            candidates = self._candidates(conn)
        if not candidates:
            raise RuntimeError("no healthy API keys available")

        last_error = None
        for cand in candidates:
            url = GEMINI_GENERATE_URL.format(model=model)
            try:
                resp = requests.post(
                    url,
                    params={"key": cand["key_value"]},
                    json=payload,
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    self.report_success(cand["id"])
                    return resp.json()
                err = self._error_type(resp.status_code, None)
                self.report_failure(cand["id"], err)
                last_error = f"key {cand['id']}: HTTP {resp.status_code}"
            except requests.RequestException as exc:  # network / timeout
                self.report_failure(cand["id"], "other")
                last_error = f"key {cand['id']}: {exc}"
            # No sleep here: failover is immediate.
        raise RuntimeError(f"all keys failed ({last_error})")

    def test_key(self, key_id: int) -> dict:
        """Minimal live call to check a key's health. Returns status dict."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        if not row:
            raise ValueError("key not found")
        row = dict(row)
        payload = {"contents": [{"parts": [{"text": "Reply with: ok"}]}]}
        try:
            resp = requests.post(
                GEMINI_GENERATE_URL.format(model="gemini-2.0-flash"),
                params={"key": row["key_value"]},
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                self.report_success(key_id)
                return {"ok": True, "status": "healthy"}
            err = self._error_type(resp.status_code, None)
            self.report_failure(key_id, err)
            return {"ok": False, "status": err, "http": resp.status_code}
        except requests.RequestException as exc:
            self.report_failure(key_id, "other")
            return {"ok": False, "status": "other", "error": str(exc)[:200]}
