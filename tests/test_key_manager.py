import os
import tempfile
import unittest
from gemini_key_manager import KeyManager, mask_key, ensure_safety_settings, default_safety_settings


class TestGeminiKeyManager(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.temp_db.close()
        self.db_path = self.temp_db.name
        self.km = KeyManager(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_mask_key(self):
        self.assertEqual(mask_key("AIzaSy123456789"), "...6789")
        self.assertEqual(mask_key("abc"), "...")
        self.assertEqual(mask_key(""), "...")

    def test_ensure_safety_settings(self):
        payload = {"contents": []}
        ensure_safety_settings(payload)
        self.assertIn("safetySettings", payload)
        self.assertEqual(payload["safetySettings"], default_safety_settings())

        custom_payload = {"contents": [], "safetySettings": []}
        ensure_safety_settings(custom_payload)
        self.assertEqual(custom_payload["safetySettings"], [])

    def test_add_and_list_keys(self):
        key1 = self.km.add_key("key-1-free-1234", label="Free 1", is_free=True)
        self.assertEqual(key1["label"], "Free 1")
        self.assertFalse(key1["is_paid"])
        self.assertEqual(key1["status"], "healthy")

        key2 = self.km.add_key("key-2-paid-5678", label="Paid 1", is_paid=True)
        self.assertTrue(key2["is_paid"])

        keys = self.km.list_keys()
        self.assertEqual(len(keys), 2)
        # Free key should come first
        self.assertEqual(keys[0]["label"], "Free 1")
        self.assertEqual(keys[1]["label"], "Paid 1")

    def test_add_duplicate_key(self):
        self.km.add_key("key-unique-1234", label="Unique", is_free=True)
        with self.assertRaises(ValueError):
            self.km.add_key("key-unique-1234", label="Duplicate", is_free=True)

    def test_key_rotation(self):
        self.km.add_key("key-a-free-1111", label="A", is_free=True)
        self.km.add_key("key-b-free-2222", label="B", is_free=True)

        k1 = self.km.get_key()
        k2 = self.km.get_key()

        # Both free keys should be retrieved
        key_ids = {k1[0], k2[0]}
        self.assertEqual(len(key_ids), 2)

    def test_report_success_and_failure(self):
        k = self.km.add_key("key-fail-test-1234", label="FailTest", is_free=True)
        key_id = k["id"]

        self.km.report_failure(key_id, "quota")
        keys = self.km.list_keys()
        self.assertEqual(keys[0]["status"], "cooling_down")
        self.assertEqual(keys[0]["consecutive_failures"], 1)

        self.km.report_success(key_id)
        keys = self.km.list_keys()
        self.assertEqual(keys[0]["status"], "healthy")
        self.assertEqual(keys[0]["consecutive_failures"], 0)

    def test_set_active_and_delete(self):
        k = self.km.add_key("key-toggle-1234", label="Toggle", is_free=True)
        key_id = k["id"]

        self.km.set_active(key_id, False)
        keys = self.km.list_keys()
        self.assertEqual(keys[0]["status"], "disabled")

        self.km.set_active(key_id, True)
        keys = self.km.list_keys()
        self.assertEqual(keys[0]["status"], "healthy")

        deleted = self.km.delete_key(key_id)
        self.assertTrue(deleted)
        self.assertEqual(len(self.km.list_keys()), 0)


if __name__ == "__main__":
    unittest.main()
