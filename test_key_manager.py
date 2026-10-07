"""Basic tests for the key selector and failover logic.

Run: python -m unittest test_key_manager.py -v
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from key_manager import KeyManager


class KeyManagerTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        self.km = KeyManager(self.db)
        self.free1 = self.km.add_key("free-key-aaaa1111", "free-1", is_paid=False)
        self.free2 = self.km.add_key("free-key-bbbb2222", "free-2", is_paid=False)
        self.paid = self.km.add_key("paid-key-cccc3333", "paid", is_paid=True)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    # ------------------------------------------------------------ selector

    def test_free_keys_come_first(self):
        cands = self.km.get_candidate_keys()
        ids = [c["id"] for c in cands]
        self.assertEqual(ids[-1], self.paid["id"], "paid key must be last")
        self.assertEqual(set(ids[:2]), {self.free1["id"], self.free2["id"]})

    def test_paid_only_when_free_exhausted(self):
        # Knock out both free keys with quota failures.
        self.km.report_failure(self.free1["id"], "quota")
        self.km.report_failure(self.free2["id"], "quota")
        cands = self.km.get_candidate_keys()
        self.assertEqual([c["id"] for c in cands], [self.paid["id"]])

    def test_degraded_key_deprioritized(self):
        self.km.report_failure(self.free1["id"], "other")
        cands = self.km.get_candidate_keys()
        free_ids = [c["id"] for c in cands if not c["is_paid"]]
        self.assertEqual(free_ids[0], self.free2["id"])

    # ------------------------------------------------------------ failover

    def test_quota_backoff_then_retry(self):
        self.km.report_failure(self.free1["id"], "quota")
        cands = self.km.get_candidate_keys()
        self.assertNotIn(self.free1["id"], [c["id"] for c in cands])
        # Expire the backoff manually and check retry_exhausted clears it.
        with self.km._conn() as conn:
            conn.execute(
                "UPDATE api_keys SET quota_exhausted_until = ? WHERE id = ?",
                (int(time.time()) - 1, self.free1["id"]),
            )
        n = self.km.retry_exhausted()
        self.assertEqual(n, 1)
        cands = self.km.get_candidate_keys()
        self.assertIn(self.free1["id"], [c["id"] for c in cands])

    def test_payment_error_flags_attention(self):
        self.km.report_failure(self.paid["id"], "payment")
        cands = self.km.get_candidate_keys()
        self.assertNotIn(self.paid["id"], [c["id"] for c in cands])
        keys = {k["id"]: k for k in self.km.list_keys()}
        self.assertEqual(keys[self.paid["id"]]["status"], "needs_attention")
        # Re-enabling clears the flag.
        self.assertTrue(self.km.set_active(self.paid["id"], True))
        keys = {k["id"]: k for k in self.km.list_keys()}
        self.assertEqual(keys[self.paid["id"]]["status"], "healthy")

    def test_success_resets_failures(self):
        self.km.report_failure(self.free1["id"], "other")
        self.km.report_failure(self.free1["id"], "other")
        self.km.report_success(self.free1["id"])
        keys = {k["id"]: k for k in self.km.list_keys()}
        self.assertEqual(keys[self.free1["id"]]["consecutive_failures"], 0)
        self.assertEqual(keys[self.free1["id"]]["status"], "healthy")

    def test_generate_fails_over_to_next_key(self):
        """Simulate: free1 429s, free2 succeeds — caller gets a result."""

        class FakeResp:
            def __init__(self, status, body=None):
                self.status_code = status
                self._body = body or {}

            def json(self):
                return self._body

        calls = []

        def fake_post(url, params=None, json=None, timeout=None):
            calls.append(params["key"])
            if params["key"] == "free-key-aaaa1111":
                return FakeResp(429)
            return FakeResp(200, {"ok": True})

        with patch("key_manager.requests.post", side_effect=fake_post):
            out = self.km.generate("gemini-2.0-flash", {"contents": []})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(calls, ["free-key-aaaa1111", "free-key-bbbb2222"])
        # free1 should now be in backoff.
        keys = {k["id"]: k for k in self.km.list_keys()}
        self.assertEqual(keys[self.free1["id"]]["status"], "cooling_down")

    def test_generate_raises_when_all_fail(self):
        class FakeResp:
            status_code = 500

            def json(self):
                return {}

        with patch("key_manager.requests.post", return_value=FakeResp()):
            with self.assertRaises(RuntimeError):
                self.km.generate("gemini-2.0-flash", {"contents": []})

    # ---------------------------------------------------------------- misc

    def test_duplicate_key_rejected(self):
        with self.assertRaises(ValueError):
            self.km.add_key("free-key-aaaa1111", "dup")

    def test_keys_are_masked_in_list(self):
        for k in self.km.list_keys():
            self.assertNotIn("key_value", k)
            self.assertTrue(k["masked"].startswith("..."))


if __name__ == "__main__":
    unittest.main()
