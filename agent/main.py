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

import html
import io
import os
import re
import time
import urllib.parse
import uuid
import zipfile

_TAG_RE = re.compile(r"<[^>]+>")


def clean_snippet(text: str) -> str:
    """Vertex snippets carry highlight markup (<b>...</b>) and HTML
    entities (&nbsp;). Strip both so answers read as plain prose."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())

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
BUCKET = os.environ.get("KHOJ_BUCKET", "khoj-ai-corpus-innovation-lab")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def gemini_endpoint():
    """Where the chat model call goes: (url, headers).

    With GEMINI_API_KEY set, the Gemini API (generativelanguage) is used.
    innovation-lab-2026 returns publisher-model 404s on the Vertex path
    for every model (verified 2026-11-02, incl. gemini-1.5-flash-002)
    while its Gemini API key route works, so the agent prefers the key
    route there. Without a key, Vertex AI with the pod's Workload
    Identity token remains the default."""
    if GEMINI_API_KEY:
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent",
            {"x-goog-api-key": GEMINI_API_KEY},
        )
    return (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/"
        f"{PROJECT}/locations/{VERTEX_LOCATION}/publishers/google/models/"
        f"{GEMINI_MODEL}:generateContent",
        {"Authorization": f"Bearer {gcp_token()}"},
    )
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
            clean_snippet(s.get("snippet", ""))
            for s in derived.get("snippets", []) if s.get("snippet")
        ]
        if snippets:
            out.append({
                "title": struct.get("title") or derived.get("title") or doc.get("id", ""),
                "snippets": snippets,
                # Corpus scope value (uid or org id): the source file
                # lives in GCS at {scope}/{title} (the web service's
                # upload layout), which the extractive path reads.
                "scope": want,
            })
        if len(out) >= page_size:
            break
    return out


_STOPWORDS = {
    "what", "all", "the", "and", "for", "are", "has", "have", "does", "did",
    "with", "from", "this", "that", "about", "into", "your", "their", "there",
    "which", "when", "where", "who", "how", "why", "can", "could", "would",
    "should", "tell", "show", "give", "list", "find", "much", "many",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]*[a-z0-9]|[a-z0-9]")
_TEXT_CACHE: dict = {}
_CACHE_LIMIT = 64


def _query_terms(question: str) -> set:
    terms = set()
    for w in _WORD_RE.findall((question or "").lower()):
        if len(w) < 3 or w in _STOPWORDS:
            continue
        terms.add(w)
        if w.endswith("s") and len(w) > 3:
            terms.add(w[:-1])
    return terms


def _score_text(text: str, terms: set) -> int:
    words = _WORD_RE.findall(text.lower())
    if not words or not terms:
        return 0
    present = set(words)
    matched = terms.intersection(present)
    hits = sum(1 for w in words if w in terms)
    return len(matched) * 12 + min(hits, 24)


def _extract_docx(raw: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return None
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = _TAG_RE.sub("", xml)
    return html.unescape(xml)


def _extract_pdf(raw: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None


def extract_text(filename: str, raw: bytes) -> str | None:
    """Extract searchable text from an uploaded file's bytes. Supported:
    plain text formats directly, HTML by block-aware tag stripping,
    DOCX via its XML (stdlib only), PDF via pypdf. Anything else returns
    None and the caller falls back to Vertex snippets for that file."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".docx":
        return _extract_docx(raw)
    if ext == ".pdf":
        return _extract_pdf(raw)
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    if ext in (".html", ".htm"):
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>|<br\s*/?>", "\n", text)
        text = _TAG_RE.sub(" ", text)
        text = html.unescape(text)
    return text


def fetch_document_text(scope: str, filename: str) -> str | None:
    """Read the caller's own uploaded file from the corpus bucket and
    return its extracted text. The object layout ({scope}/{filename})
    and bucket are the web service's upload contract; the agent's
    Workload Identity carries storage access to that bucket. A small
    in-process cache avoids refetching on every chat turn (a re-upload
    under the same name is picked up on pod restart)."""
    if not scope or not filename:
        return None
    cache_key = (scope, filename)
    if cache_key in _TEXT_CACHE:
        return _TEXT_CACHE[cache_key]
    obj = urllib.parse.quote(f"{scope}/{filename}", safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{obj}?alt=media"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {gcp_token()}"},
            timeout=(5, 20),
        )
        if resp.status_code != 200 or len(resp.content) > 12_000_000:
            return None
        text = extract_text(filename, resp.content)
    except Exception as e:
        print(f"fetch_document_text({filename}): {e!r}", flush=True)
        return None
    if text:
        if len(_TEXT_CACHE) >= _CACHE_LIMIT:
            _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
        _TEXT_CACHE[cache_key] = text[:300_000]
    return text


def _looks_like_heading(block: str, next_block: str | None) -> bool:
    if len(block) > 90:
        return False
    if block.endswith(":"):
        return True
    letters = [c for c in block if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.75:
        return len(block) >= 3
    if next_block and len(next_block) > max(120, int(len(block) * 1.6)):
        return not block.endswith((".", ",", ";", "!", "?"))
    return False


def _sections(text: str) -> list[tuple[str, str]]:
    lines = [line.strip() for line in (text or "").splitlines()]
    blocks = [line for line in lines if line]
    if not blocks:
        return []
    sections: list[tuple[str, str]] = []
    heading = ""
    current: list[str] = []
    current_len = 0

    def flush():
        if current:
            sections.append((heading, "\n".join(current)))

    for index, block in enumerate(blocks):
        next_block = blocks[index + 1] if index + 1 < len(blocks) else None
        if _looks_like_heading(block, next_block) and current_len >= 30:
            flush()
            heading = block
            current = []
            current_len = 0
            continue
        if _looks_like_heading(block, next_block) and not current:
            heading = block
            continue
        current.append(block)
        current_len += len(block) + 1
        if current_len >= 1800:
            flush()
            heading = ""
            current = []
            current_len = 0
    flush()
    return sections


def pick_passages(text: str, question: str, budget: int = 2400) -> tuple[str, int]:
    """Return the most relevant complete section(s), not snippets.
    Sections are heading + following text, scored by distinct query
    terms and density. Returned in document order within budget."""
    terms = _query_terms(question)
    if not terms:
        return "", 0
    scored = []
    for pos, (heading, body) in enumerate(_sections(text)):
        full = (heading + "\n" + body).strip() if heading else body
        score = _score_text(full, terms)
        if score > 0:
            scored.append((pos, score, full))
    if not scored:
        return "", 0
    best = max(score for _, score, _ in scored)
    chosen = []
    used = 0
    for _, score, full in sorted(scored):
        if used and used + len(full) + 2 > budget:
            continue
        chosen.append(full)
        used += len(full) + 2
        if used >= budget:
            break
    return "\n".join(chosen), best


def extractive_answer(question: str, sources: list) -> str:
    """Complete extractive answer: retrieve the actual source files
    and quote the best full sections. Earlier version stitched Vertex's
    ~200-character snippets (first document only, max 600 chars), which
    produced visibly unfinished answers. Last-resort fallback still
    uses snippets across sources if no file can be fetched/parsed."""
    candidates: list[tuple[int, int, str, str]] = []
    for index, source in enumerate(sources[:3]):
        text = fetch_document_text(source.get("scope", ""), source.get("title", ""))
        if not text:
            continue
        passages, best = pick_passages(text, question)
        if passages:
            candidates.append((index, best, source["title"], passages))
    chunks: list[str] = []
    if candidates:
        top_best = max(best for _, best, _, _ in candidates)
        for _, best, title, passages in sorted(candidates):
            if chunks and best < top_best * 0.5:
                continue
            chunks.append(f"From {title}:\n{passages}")
            if len(chunks) >= 2:
                break
    if not chunks:
        chunks = [
            f"From {s['title']}: " + " ".join(s["snippets"])[:800]
            for s in sources if s.get("snippets")
        ]
    if not chunks:
        return (
            "I found matching documents, but could not extract readable "
            "passages from them."
            "\n\n(chat is running in extractive mode right now)"
        )
    return "\n\n".join(chunks) + "\n\n(chat is running in extractive mode right now)"


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
    url, headers = gemini_endpoint()
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=45)
        if resp.status_code != 200:
            raise RuntimeError(resp.text[:200])
        parts = resp.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if text:
            return text
        raise RuntimeError("Gemini returned an empty answer")
    except Exception as e:
        print(f"gemini_answer: falling back to extractive mode: {e!r}", flush=True)
    return extractive_answer(question, sources)


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
