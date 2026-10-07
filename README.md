# Gemini Key Manager

Free-first Gemini API key rotation with health monitoring, instant failover,
and an OpenAI-compatible proxy endpoint.

## How it works

- **Free-first rotation** — free keys are always tried before the paid key.
  The paid key is only used when every free key is exhausted, disabled, or cooling down.
- **Health tracking** — every key tracks total requests, failures, last
  success/failure. The management API and UI show a status per key:
  `healthy`, `degraded`, `cooling_down`, `disabled`, `needs_attention`.
- **Instant failover** — if a key returns 429 / 402 / 5xx (or the request
  errors), the next healthy key is tried immediately. No delay to the caller.
- **Retry / backoff** — quota failures (429) put a key in exponential backoff
  (5 min → 15 min → 1 h → 4 h). Expired backoffs are cleared automatically so
  keys are retried. Payment failures (402) flag the key `needs_attention`;
  re-enable it from the UI once fixed.

## Setup

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Optional env vars:

- `KEYS_DB` — SQLite path (default `keys.db`)
- `ADMIN_TOKEN` — if set, management endpoints (`/keys*`) require the
  `x-admin-token` header. The proxy endpoints never require it.

## Adding keys

Open `http://localhost:8000/` in a browser and use the **Add key** form
(label + Free/Paid toggle), or use the API:

```bash
# add a free key
curl -X POST localhost:8000/keys \
  -H 'Content-Type: application/json' \
  -d '{"key":"YOUR_KEY","label":"free-1","is_paid":false}'

# add the paid fallback key
curl -X POST localhost:8000/keys \
  -H 'Content-Type: application/json' \
  -d '{"key":"YOUR_PAID_KEY","label":"paid","is_paid":true}'

# list with health
curl localhost:8000/keys

# test a key
curl -X POST localhost:8000/keys/1/test

# disable / re-enable
curl -X PATCH localhost:8000/keys/1 -H 'Content-Type: application/json' -d '{"is_active":false}'

# delete
curl -X DELETE localhost:8000/keys/1
```

Key values are never returned by the API — only a masked suffix (`...abcd`).

## Proxy endpoints

Point any OpenAI-compatible client at this server:

```bash
curl -X POST localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"Hello"}]}'
```

Gemini-native passthrough also works:

```bash
curl -X POST 'localhost:8000/v1beta/models/gemini-2.0-flash:generateContent' \
  -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"Hello"}]}]}'
```

## Tests

```bash
python -m unittest test_key_manager.py -v
```
