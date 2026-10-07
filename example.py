"""Runnable demo for gemini-key-manager.

Needs at least one real key: either set GEMINI_API_KEY in the environment
or edit the add_key() calls below.
"""
import os

from gemini_key_manager import (
    KeyManager,
    ensure_safety_settings,
    mask_key,
)

DB = "/tmp/gkm-demo-keys.db"
if os.path.exists(DB):
    os.remove(DB)  # fresh demo run

km = KeyManager(db_path=DB)

# 1. Add keys ---------------------------------------------------------------
env_key = os.environ.get("GEMINI_API_KEY", "").strip()
added = 0
if env_key:
    km.add_key(env_key, label="env-key", is_free=True)
    added += 1
# Add more like this:
# km.add_key("AIza...", label="Free 2", is_free=True)
# km.add_key("AIza...", label="Paid fallback", is_paid=True)

print("keys in pool:")
for k in km.list_keys():
    print(f"  id={k['id']} label={k['label']} masked={k['key_masked']} "
          f"paid={k['is_paid']} status={k['status']}")

if not added:
    print("\nNo real key available — set GEMINI_API_KEY to run a live call.")
    print("Admin helpers demo (no network needed):")
    print("  mask_key('AIzaSyD-EXAMPLE-KEY') ->", mask_key("AIzaSyD-EXAMPLE-KEY"))
    raise SystemExit(0)

# 2. Make a request ---------------------------------------------------------
payload = {
    "contents": [{"role": "user",
                  "parts": [{"text": "Reply with the single word: ok"}]}],
    "generationConfig": {"temperature": 0, "maxOutputTokens": 5},
}
ensure_safety_settings(payload)

url = ("https://generativelanguage.googleapis.com/v1beta/models/"
       "gemini-3.1-flash-lite:generateContent")
result = km.post_json(url, payload, source="demo")

if result is None:
    print("\nAll keys failed — check key health with km.list_keys().")
else:
    parts = result["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if p.get("text"))
    print("\nGemini replied:", text.strip())

# 3. Usage analytics --------------------------------------------------------
print("\nusage by source:")
for row in km.usage_by_source():
    print(f"  {row['source']}: {row['requests']} requests, "
          f"{row['successes']} ok, {row['failures']} failed")

# 4. Health check a single key ----------------------------------------------
first = km.list_keys()[0]
print("\ntest_key(%d) ->" % first["id"], km.test_key(first["id"]))
