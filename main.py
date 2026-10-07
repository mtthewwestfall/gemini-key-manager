"""Gemini key manager — FastAPI proxy with free-first key rotation.

Endpoints:
  POST /v1/chat/completions                 OpenAI-compatible chat proxy
  POST /v1beta/models/{model}:generateContent   Gemini-native proxy
  GET    /keys        list keys + health (masked)
  POST   /keys        add {key, label, is_paid}
  DELETE /keys/{id}   remove
  PATCH  /keys/{id}   {is_active}
  POST   /keys/{id}/test   live health check
  GET    /          management UI

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from key_manager import KeyManager

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DB_PATH = os.environ.get("KEYS_DB", "keys.db")

app = FastAPI(title="gemini-key-manager")
km = KeyManager(DB_PATH)


def _check_admin(request: Request):
    if ADMIN_TOKEN and request.headers.get("x-admin-token") != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="bad admin token")


# ------------------------------------------------------------------ models

class AddKey(BaseModel):
    key: str
    label: str = ""
    is_paid: bool = False


class PatchKey(BaseModel):
    is_active: bool


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "gemini-2.0-flash"
    messages: list[ChatMessage]
    max_tokens: int | None = None
    temperature: float | None = None


# ------------------------------------------------------------- proxy logic

def _openai_to_gemini(body: ChatCompletionRequest) -> tuple[str, dict]:
    """Convert an OpenAI chat request to a Gemini generateContent payload."""
    model = body.model.replace("models/", "")
    contents = []
    system_text = None
    for m in body.messages:
        if m.role == "system":
            system_text = (system_text + "\n" if system_text else "") + m.content
        else:
            contents.append(
                {"role": "model" if m.role == "assistant" else "user",
                 "parts": [{"text": m.content}]}
            )
    payload: dict = {"contents": contents}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    gen_cfg: dict = {}
    if body.max_tokens:
        gen_cfg["maxOutputTokens"] = body.max_tokens
    if body.temperature is not None:
        gen_cfg["temperature"] = body.temperature
    if gen_cfg:
        payload["generationConfig"] = gen_cfg
    return model, payload


def _gemini_to_openai(model: str, gemini_resp: dict) -> dict:
    text = ""
    try:
        text = gemini_resp["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = ""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
def chat_completions(body: ChatCompletionRequest):
    try:
        model, payload = _openai_to_gemini(body)
        gemini_resp = km.generate(model, payload)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return _gemini_to_openai(model, gemini_resp)


@app.post("/v1beta/models/{model}:generateContent")
async def generate_content(model: str, request: Request):
    payload = await request.json()
    try:
        return km.generate(model, payload)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ---------------------------------------------------------- management API

@app.get("/keys")
def list_keys(request: Request):
    _check_admin(request)
    km.retry_exhausted()
    return {"keys": km.list_keys()}


@app.post("/keys")
def add_key(body: AddKey, request: Request):
    _check_admin(request)
    try:
        return km.add_key(body.key, body.label, body.is_paid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/keys/{key_id}")
def delete_key(key_id: int, request: Request):
    _check_admin(request)
    if not km.delete_key(key_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@app.patch("/keys/{key_id}")
def patch_key(key_id: int, body: PatchKey, request: Request):
    _check_admin(request)
    if not km.set_active(key_id, body.is_active):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@app.post("/keys/{key_id}/test")
def test_key(key_id: int, request: Request):
    _check_admin(request)
    try:
        return km.test_key(key_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# -------------------------------------------------------------------- UI

UI_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gemini Key Manager</title>
<style>
body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:16px}
h1{font-size:20px}
.card{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:12px;margin:10px 0}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:600}
.free{background:#0f5132;color:#b6f0c9}.paid{background:#5a3d00;color:#ffd97a}
.st-healthy{color:#4ade80}.st-degraded{color:#fbbf24}.st-cooling_down{color:#60a5fa}
.st-disabled{color:#9ca3af}.st-needs_attention{color:#f87171}
button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:8px 12px;margin:4px 4px 4px 0;cursor:pointer}
button.danger{background:#b91c1c}button.ghost{background:#374151}
input,select{background:#222;border:1px solid #444;color:#eee;border-radius:8px;padding:8px;margin:4px 4px 4px 0}
.stats{font-size:13px;color:#aaa}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
</style></head><body>
<h1>&#128272; Gemini Key Manager</h1>
<div class="card">
<h3>Add key</h3>
<div class="row">
<input id="k" type="password" placeholder="API key" style="flex:2;min-width:200px">
<input id="l" placeholder="label" style="flex:1;min-width:120px">
<select id="p"><option value="0">Free</option><option value="1">Paid</option></select>
<button onclick="addKey()">Add</button>
</div></div>
<div id="list"></div>
<script>
const token = localStorage.getItem('gkm_token') || '';
async function api(m,u,b){
  const r = await fetch(u,{method:m,headers:{'Content-Type':'application/json','x-admin-token':token},body:b?JSON.stringify(b):undefined});
  if(!r.ok){const t=await r.text();alert('Error '+r.status+': '+t);throw 0;}
  return r.json();
}
function badge(k){return `<span class="badge ${k.is_paid?'paid':'free'}">${k.is_paid?'PAID':'FREE'}</span>`;}
async function load(){
  const d = await api('GET','/keys').catch(()=>null);
  if(!d){const t=prompt('Admin token (blank if none set):');if(t===null)return;localStorage.setItem('gkm_token',t);return load();}
  document.getElementById('list').innerHTML = d.keys.map(k=>`
  <div class="card">
    <div class="row"><b>${k.label||('key '+k.id)}</b> ${badge(k)}
      <span class="st-${k.status}">&#9679; ${k.status.replace('_',' ')}</span>
      <code>${k.masked}</code></div>
    <div class="stats">requests: ${k.total_requests} &middot; failures: ${k.total_failures}
      ${k.last_success_at?' &middot; last ok: '+new Date(k.last_success_at*1000).toLocaleString():''}
      ${k.quota_exhausted_until*1000>Date.now()?' &middot; backoff until '+new Date(k.quota_exhausted_until*1000).toLocaleTimeString():''}</div>
    <div class="row">
      <button onclick="testKey(${k.id})">Test</button>
      <button class="ghost" onclick="toggleKey(${k.id},${k.is_active?0:1})">${k.is_active?'Disable':'Enable'}</button>
      <button class="danger" onclick="delKey(${k.id})">Delete</button>
    </div>
  </div>`).join('') || '<p>No keys yet.</p>';
}
async function addKey(){
  await api('POST','/keys',{key:document.getElementById('k').value,label:document.getElementById('l').value,is_paid:document.getElementById('p').value==='1'});
  document.getElementById('k').value='';load();
}
async function delKey(id){if(confirm('Delete key '+id+'?')){await api('DELETE','/keys/'+id);load();}}
async function toggleKey(id,a){await api('PATCH','/keys/'+id,{is_active:!!a});load();}
async function testKey(id){const r=await api('POST','/keys/'+id+'/test');alert(JSON.stringify(r));load();}
setInterval(load,30000);load();
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return UI_HTML
