# gemini-key-manager

A standalone, zero-dependency Gemini API key manager with rotation, health
tracking, and transparent failover. Built for stretching free-tier Gemini
keys as far as they'll go — free keys are tried first, a paid key is only
touched when every free key is exhausted.

Stdlib only: `sqlite3`, `hashlib`, `json`, `threading`, `urllib`, `datetime`.
No pip install required.

## Quick start

```python
from gemini_key_manager import KeyManager, ensure_safety_settings

km = KeyManager(db_path="keys.db")

# Add keys (deduped by hash; values are never returned by admin helpers)
km.add_key("AIza...", label="Free 1", is_free=True)
km.add_key("AIza...", label="Free 2", is_free=True)
km.add_key("AIza...", label="Paid", is_paid=True)

# Make a request — key selection, rotation, retries, and health
# tracking are all handled automatically.
payload = {
    "contents": [{"role": "user",
                  "parts": [{"text": "Say hi in one sentence."}]}],
}
ensure_safety_settings(payload)  # optional: BLOCK_NONE for explicit content
result = km.post_json(
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent",
    payload,
    source="my-app",          # recorded in the usage log
)
print(result["candidates"][0]["content"]["parts"][0]["text"])
```

Run `python example.py` for a runnable demo (needs at least one real key in
the `GEMINI_API_KEY` env var or added via `add_key`).

## How it works

**Selection — free first, round-robin.** Eligible keys are ordered free tier
first, then least-recently-used, then fewest failures. Every `get_key()` /
`post_json()` advances rotation, so load spreads across all keys instead of
hammering the first one.

**Failover — one failure, instant switch.** `post_json()` tries each eligible
key in order. On any failure the key's health is recorded and the next key
is tried immediately — a dead key is never retried for the same request. If
*every* key is in a backoff window, all active keys are tried ignoring
backoff as a last resort rather than failing the request outright.

**Health model per key:**

| Event | Effect |
|---|---|
| Any success | `consecutive_failures` reset, backoff cleared |
| 429 / quota | Exponential backoff: 5m → 15m → 1h → 4h |
| 402 / payment | `needs_attention = 1`; key skipped until re-enabled |
| Other (5xx, timeout, network) | 2-minute backoff — next request skips it immediately, key recovers fast if transient |

**Source tracking.** Every attempt is logged (`key_id`, `source`, success).
`usage_by_source()` shows which app is burning through keys:

```python
km.usage_by_source()   # [{"source": "my-app", "requests": 42, ...}]
km.get_usage(limit=50) # individual recent attempts
```

**Thread-safe.** An instance lock guards selection and counter updates;
network I/O happens outside the lock. Each call opens and closes its own
sqlite connection.

**Secrets.** Key values live in the local sqlite DB. Admin helpers
(`list_keys()`, etc.) return masked values (`...abcd`) only. Never log full
key values.

## API reference

### `KeyManager(db_path="keys.db", legacy_key=None)`

- `db_path` — sqlite file holding keys, health, and usage log.
- `legacy_key` — optional existing key adopted as the initial paid key
  (label `"legacy-env"`) on first run. Defaults to the `GEMINI_API_KEY`
  environment variable.

### Request path

- `post_json(url, payload, timeout=30, source="unknown") -> dict | None` —
  POST JSON with transparent rotation/failover. Returns parsed JSON or
  `None` when every key failed.
- `get_key(source="unknown") -> (key_id, key_value) | None` — next key in
  rotation, or `None` when the pool is exhausted.
- `report_success(key_id)` / `report_failure(key_id, error_type)` —
  `error_type` is `"quota"`, `"payment"`, or `"other"`. Used automatically
  by `post_json()`; call manually if you roll your own request loop.
- `retry_exhausted() -> int` — clear expired backoff windows; returns keys
  reopened.

### Admin

- `add_key(key_value, label="", is_paid=False, is_free=None) -> dict` —
  `is_free=True` is shorthand for `is_paid=False`. Raises `ValueError` on
  empty/duplicate keys. Returns the masked public row.
- `list_keys() -> list[dict]` — all keys with masked values and a `status`
  field: `healthy`, `degraded`, `cooling_down`, `disabled`,
  `needs_attention`.
- `delete_key(key_id) -> bool`
- `set_active(key_id, is_active) -> dict | None` — re-enabling clears the
  attention flag and backoff.
- `test_key(key_id) -> dict | None` — minimal live call; returns health,
  never the key.

### Usage analytics

- `get_usage(limit=100) -> list[dict]` — recent attempts with timestamp,
  source, key label, success.
- `usage_by_source(limit=100) -> list[dict]` — aggregate counts per source.

### Helpers (module level)

- `mask_key(key_value) -> str` — `"...abcd"`.
- `default_safety_settings() -> list[dict]` — sexually explicit `BLOCK_NONE`,
  everything else `BLOCK_MEDIUM_AND_ABOVE`.
- `ensure_safety_settings(payload) -> dict` — inject the defaults when the
  caller didn't specify `safetySettings`; explicit overrides respected.

## Multi-app proxy pattern

Point every app at one shared instance instead of giving each app its own
keys — one place to manage rotation, no per-app key changes:

```python
# your tiny proxy server (Flask/FastAPI/Starlette)
from gemini_key_manager import KeyManager, ensure_safety_settings

km = KeyManager("/data/keys.db")

@app.post("/v1beta/models/{model}:generateContent")
def proxy(model: str, body: dict, request: Request):
    require_token(request)  # your auth
    source = request.headers.get("X-Source", "unknown")
    ensure_safety_settings(body)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    data = km.post_json(url, body, source=source)
    if data is None:
        raise HTTPException(502, "all_keys_failed")
    return data
```

Each app sends `X-Source: my-app-name`; `usage_by_source()` then shows
exactly which app is consuming quota.

## License

MIT — do what you want with it.
