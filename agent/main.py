"""Khoj A2A agent - conversational Q&A over a user's Khoj corpus.

Implements the A2A protocol (JSON-RPC 2.0): an AgentCard at
/.well-known/agent-card.json and message/send (+ message/stream) on the
root endpoint. Each request carries the caller's Firebase ID token as a
Bearer credential; the agent verifies it, searches ONLY that caller's
corpus (or a business corpus they belong to, via message metadata
orgId) in Vertex AI Search, and grounds a Gemini answer on the
retrieved snippets.

This is the conversational surface for Khoj: the web app's chat panel
talks to this service as an A2A client, and any external A2A client can
do the same.
"""

import os
import time
import uuid

import firebase_admin
import requests
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from firebase_admin import auth as fb_auth
from firebase_admin import firestore as fb_firestore
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest

from app.orgs import FirestoreOrgService

PROJECT = os.environ.get("GCP_PROJECT", "innovation-lab-2026")
ENGINE_ID = os.environ.get("KHOJ_ENGINE_ID", "khoj-search")
DATASTORE_ID = os.environ.get("KHOJ_DATASTORE_ID", "")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DE_BASE = (
    f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT}"
    "/locations/global/collections/default_collection"
)
PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", "")

try:
    firebase_admin.get_app()
except ValueError:
    firebase_admin.initialize_app()

app = FastAPI(title="Khoj A2A Agent")

_org_service = None
_history: dict[str, list] = {}  # contextId -> [{q, a}] (POC-scale, in-memory)
_creds = None


def org_service() -> FirestoreOrgService:
    global _org_service
    if _org_service is None:
        _org_service = FirestoreOrgService(fb_firestore.client())
    return _org_service


def gcp_token() -> str:
    global _creds
    if _creds is None:
        _creds, _ = google_auth_default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _creds.valid:
        _creds.refresh(GoogleAuthRequest())
    return _creds.token


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise PermissionError("Missing ID token")
    decoded = fb_auth.verify_id_token(authorization.split(" ", 1)[1])
    return {"uid": decoded["uid"], "email": decoded.get("email", "")}


def authorize_scope(user: dict, org_id: str | None) -> tuple:
    """Membership/approval gate for business corpora; returns the
    (structData key, expected value) pair this caller may see. Scope is
    enforced by post-filtering: the data store's auto-derived schema
    does not expose user_id/org_id as filterable fields."""
    if org_id:
        svc = org_service()
        if svc.membership(user["uid"], org_id) is None:
            raise PermissionError("Not a member of this business")
        org = svc.get_org(org_id)
        if org and org.get("status") != "approved":
            raise PermissionError("This business is pending approval")
        return "org_id", org_id
    return "user_id", user["uid"]


def search_snippets(query: str, user: dict, org_id: str | None, page_size: int = 5) -> list:
    """Standard-edition Vertex search: titles + snippets for grounding."""
    key, want = authorize_scope(user, org_id)
    url = f"{DE_BASE}/engines/{ENGINE_ID}/servingConfigs/default_search:search"
    payload = {
        "query": query,
        "pageSize": min(page_size * 5, 50),
        "spellCorrectionSpec": {"mode": "AUTO"},
        "contentSearchSpec": {"snippetSpec": {"returnSnippet": True}},
    }
    resp = requests.post(
        url, json=payload,
        headers={"Authorization": f"Bearer {gcp_token()}"}, timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Search backend: {resp.text[:200]}")
    out = []
    for r in resp.json().get("results", []):
        doc = r.get("document", {})
        derived = doc.get("derivedStructData", {})
        struct = doc.get("structData", {})
        if struct.get(key) != want:
            continue  # another corpus's document - never used for grounding
        snippets = [
            s.get("snippet", "") for s in derived.get("snippets", []) if s.get("snippet")
        ]
        if snippets:
            out.append({
                "title": struct.get("title") or derived.get("title") or doc.get("id", ""),
                "snippets": snippets,
            })
        if len(out) >= page_size:
            break
    return out


def gemini_answer(question: str, sources: list, history: list) -> str:
    """Ground a Gemini answer on the retrieved snippets. Falls back to
    an extractive answer when the model call is unavailable, so chat
    never hard-fails."""
    excerpts = "\n\n".join(
        f"[{i + 1}] {s['title']}\n" + "\n".join(s["snippets"])
        for i, s in enumerate(sources)
    )
    if not sources:
        return (
            "I couldn't find anything about that in the selected documents. "
            "Try different words, or upload the document that covers it."
        )
    history_text = "\n".join(f"User: {h['q']}\nAssistant: {h['a']}" for h in history[-4:])
    prompt = (
        "You are Khoj, a document assistant. Answer the question using ONLY "
        "the document excerpts below. Cite excerpts inline like [1], [2]. "
        "If the excerpts do not contain the answer, say so plainly. Be "
        "concise and direct.\n\n"
        f"Conversation so far:\n{history_text or '(none)'}\n\n"
        f"Document excerpts:\n{excerpts}\n\nQuestion: {question}"
    )
    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/"
        f"{PROJECT}/locations/{VERTEX_LOCATION}/publishers/google/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    try:
        resp = requests.post(
            url, json=body,
            headers={"Authorization": f"Bearer {gcp_token()}"}, timeout=45,
        )
        if resp.status_code != 200:
            raise RuntimeError(resp.text[:200])
        parts = resp.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if text:
            return text
    except Exception:
        pass
    first = sources[0]
    return (
        "From " + first["title"] + ": " + " ".join(first["snippets"])[:600]
        + "\n\n(chat is running in extractive mode right now)"
    )


AGENT_CARD = {
    "name": "Khoj Document Assistant",
    "description": "Ask questions over your Khoj documents - answers are grounded in your corpus (or your business's corpus) with citations.",
    "url": PUBLIC_URL,
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "capabilities": {"streaming": True, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "securitySchemes": {
        "firebaseBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    },
    "security": [{"firebaseBearer": []}],
    "skills": [
        {
            "id": "answer-questions",
            "name": "Answer questions over documents",
            "description": "Conversational Q&A grounded in the caller's Khoj corpus; pass metadata.orgId to ask over a business corpus.",
            "tags": ["documents", "rag", "search", "qa"],
            "examples": [
                "What does the manual say about installation?",
                "Summarize the billing policy.",
            ],
        }
    ],
}


@app.get("/.well-known/agent-card.json")
def agent_card():
    return AGENT_CARD


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_message(params: dict, user: dict) -> dict:
    msg = params.get("message") or {}
    parts = msg.get("parts") or []
    text = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text").strip()
    if not text:
        raise ValueError("Message has no text part")
    context_id = msg.get("contextId") or uuid.uuid4().hex
    metadata = msg.get("metadata") or {}
    org_id = metadata.get("orgId") or None
    sources = search_snippets(text, user, org_id)
    history = _history.setdefault(context_id, [])
    answer = gemini_answer(text, sources, history)
    history.append({"q": text, "a": answer})
    del history[:-6]
    return {
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "role": "agent",
        "contextId": context_id,
        "parts": [{"kind": "text", "text": answer}],
        "metadata": {"sources": [s["title"] for s in sources]},
    }


@app.post("/")
@app.post("/a2a")
async def a2a(request: Request, authorization: str | None = Header(default=None)):
    body = await request.json()
    req_id = body.get("id")
    method = body.get("method", "")
    try:
        user = require_user(authorization)
    except Exception:
        return JSONResponse(_rpc_error(req_id, -32001, "Unauthorized: valid Firebase ID token required"))
    if method == "message/send":
        try:
            result = _handle_message(body.get("params") or {}, user)
        except PermissionError as e:
            return JSONResponse(_rpc_error(req_id, -32003, str(e)))
        except ValueError as e:
            return JSONResponse(_rpc_error(req_id, -32602, str(e)))
        except Exception as e:  # backend failures surface as internal errors
            return JSONResponse(_rpc_error(req_id, -32603, str(e)[:300]))
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "message/stream":
        def events():
            import json as _json
            task = {
                "kind": "task", "id": uuid.uuid4().hex,
                "contextId": (body.get("params") or {}).get("message", {}).get("contextId") or uuid.uuid4().hex,
                "status": {"state": "working"},
            }
            yield f"data: {_json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'kind': 'statusUpdate', 'taskId': task['id'], 'contextId': task['contextId'], 'status': {'state': 'working'}, 'final': False}})}\n\n"
            try:
                result = _handle_message(body.get("params") or {}, user)
                yield f"data: {_json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': result})}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps(_rpc_error(req_id, -32603, str(e)[:300]))}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return JSONResponse(_rpc_error(req_id, -32601, f"Method not found: {method}"))
