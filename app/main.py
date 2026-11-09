"""Khoj.ai backend.

Product model: every signed-in user uploads their OWN documents and can
search only their own corpus. Businesses (orgs, app/orgs.py) are the
second flavor: a business has N member users who share one corpus.
Isolation is enforced server-side: OpenRAG is the single retrieval
engine, and every document Khoj sends it is stored under a unique name
derived from the scope + filename ("<docId>--<filename>"). Every search
is filtered to exactly the caller's document names, built only from the
verified token (+ a membership check for org corpora) - scope ids never
come from raw client assertions. The original file is also kept in the
configured storage backend (app/storage.py: local disk by default, or a
GCS bucket when KHOJ_STORAGE=gcs) as the source of truth for
re-ingestion and full-text handoff.

Auth is built in (app/auth.py): email + password accounts in SQLite,
sessions as app-issued JWTs. No Firebase, no Google libraries on the
default path; all state lives in <KHOJ_DATA_DIR>/khoj.db plus the
storage backend.

Surfaces:
  Web UI + API (static app in app/static):
    POST /api/auth/register  Create an account (returns a token)
    POST /api/auth/login     Sign in (returns a token)
    GET  /api/config         Deployment info (auth mode, storage backend)
    GET  /api/me             Who am I (+ super-admin flag)
    POST /api/search         Search my documents (OpenRAG)
    GET  /api/documents      List my documents
    POST /api/upload         Upload a document into my corpus
  MCP server (app/mcp_server.py) for the user's own agent:
    POST /mcp              Streamable-HTTP MCP endpoint (Bearer token);
                           tools: search_documents, list_my_documents
"""

import hashlib
import html
import io
import json
import os
import re
import time
import uuid

import requests
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth as khoj_auth
from .db import Users, connect as db_connect
from .mcp_server import handle_rpc
from .orgs import PLATFORM_ORG_ID, ROLES, UPLOAD_ROLES, SqliteOrgService
from .storage import get_storage
# OpenRAG is the single retrieval engine (in-cluster service).
OPENRAG_URL = os.environ.get("OPENRAG_URL", "http://openrag-backend:8000")
# NOTE: the OpenRAG backend serves UNPREFIXED routes (/search, /files,
# /router/upload_ingest, /tasks/...). The /api/* prefix exists only on the
# OpenRAG frontend's proxy (frontend/app/api/[...path] strips it). Calling
# the backend with /api/* returns 404 - learned live 2026-11-03.
#
# Relevance floor for web search results. Measured live 2026-11-03:
# unrelated queries return baseline chunks scoring 1.25-1.29 on every
# corpus tried, while genuine matches scored 1.65+. Without a floor,
# kNN returns the caller's nearest documents for ANY query, so junk
# searches listed documents instead of returning nothing.
MIN_SCORE = float(os.environ.get("KHOJ_MIN_SCORE", "1.5"))

SUPER_ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("KHOJ_SUPER_ADMIN_EMAIL", "").split(",")
    if e.strip()
}

app = FastAPI(title="Khoj.ai")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


_TAG_RE = re.compile(r"<[^>]+>")


def clean_snippet(text: str) -> str:
    """Search snippets can carry highlight markup (<b>...</b>) and HTML
    entities (&nbsp;). Strip both so results read as plain prose."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        return khoj_auth.verify_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


_users = Users()


def _maybe_bootstrap_super_admin(user: dict) -> None:
    """Accounts whose email is configured as a platform super admin
    (KHOJ_SUPER_ADMIN_EMAIL) join the platform org as admins — the same
    super-admin mechanism as before, bootstrapped from config instead
    of a manual database edit."""
    if (user.get("email") or "").lower() in SUPER_ADMIN_EMAILS:
        try:
            org_service().ensure_platform_org(user)
        except Exception:
            pass


class AuthBody(BaseModel):
    email: str
    password: str


def _auth_response(user: dict) -> dict:
    _maybe_bootstrap_super_admin(user)
    return {
        "token": khoj_auth.issue_token(user["uid"], user["email"]),
        "email": user["email"],
        "uid": user["uid"],
    }


@app.post("/api/auth/register")
def auth_register(body: AuthBody):
    email = body.email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise HTTPException(status_code=422, detail="A valid email is required")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if _users.get_by_email(email):
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    try:
        user = _users.create_user(email, body.password)
    except Exception:
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    return _auth_response(user)


@app.post("/api/auth/login")
def auth_login(body: AuthBody):
    user = _users.authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    return _auth_response(user)


def document_id(scope: str, filename: str) -> str:
    """Stable per-(scope, filename) document id."""
    return "doc-" + hashlib.sha256(f"{scope}/{filename}".encode()).hexdigest()[:24]


def openrag_filename(doc_id: str, filename: str) -> str:
    """The name a document carries inside OpenRAG. The doc-id prefix
    makes it unique per scope, so an OpenRAG filename filter is an
    exact corpus boundary (two users can both upload 'resume.pdf')."""
    return f"{doc_id}--{filename}"


def scope_of(user: dict, org_id: str | None) -> tuple[str, str]:
    """(storage scope prefix, scope key) for a corpus. Org
    authorization (membership + approval) is checked by callers via
    org_role/org_active before this is used for access."""
    if org_id:
        return f"orgs/{org_id}", f"org:{org_id}"
    return f"users/{user['uid']}", f"user:{user['uid']}"


GLOBAL_SCOPE_KEY = f"org:{PLATFORM_ORG_ID}"


def _corpus_records(user: dict, org_id: str | None) -> list:
    """Upload records for the caller's own corpus PLUS the global
    corpus: the platform org's documents, which every signed-in user
    and every business may query (but never another business's)."""
    records = _scope_records(user, org_id)
    if org_id != PLATFORM_ORG_ID:
        try:
            global_records = org_service().list_uploads(GLOBAL_SCOPE_KEY)
        except Exception:
            global_records = []
        seen = {r.get("docId") for r in records}
        for rec in global_records:
            if rec.get("docId") in seen:
                continue
            rec["_openrag_name"] = openrag_filename(
                rec.get("docId", ""), rec.get("filename", "")
            )
            rec["_global"] = True
            records.append(rec)
    return records


def _scope_records(user: dict, org_id: str | None) -> list:
    """Upload records for the caller's corpus, each enriched with its
    OpenRAG filename. These records are the corpus membership list."""
    _, scope_key = scope_of(user, org_id)
    try:
        records = org_service().list_uploads(scope_key)
    except Exception:
        records = []
    for rec in records:
        rec["_openrag_name"] = openrag_filename(
            rec.get("docId", ""), rec.get("filename", "")
        )
    return records


_org_service = None


def org_service():
    global _org_service
    if _org_service is None:
        _org_service = SqliteOrgService(db_connect)
    return _org_service


def org_role(user: dict, org_id: str, allowed=ROLES) -> str:
    svc = org_service()
    if svc.get_org(org_id) is None:
        raise HTTPException(status_code=404, detail="Business not found")
    role = svc.membership(user["uid"], org_id)
    if role is None:
        raise HTTPException(status_code=403, detail="Not a member of this business")
    if role not in allowed:
        raise HTTPException(status_code=403, detail="Your role cannot do this")
    return role


def is_super_admin(user: dict) -> bool:
    """Super admins are the admins of the platform org (Khoj.ai itself)."""
    try:
        return org_service().membership(user["uid"], PLATFORM_ORG_ID) == "admin"
    except Exception:
        return False


def require_super_admin(user: dict) -> None:
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Super admin only")


def org_active(user: dict, org_id: str) -> None:
    """Org signup is approval-gated: until a super admin approves the
    org, its corpus is closed (its admin can still brand and invite)."""
    org = org_service().get_org(org_id)
    if org and org.get("status") != "approved" and not is_super_admin(user):
        raise HTTPException(status_code=403, detail="This business is pending approval")


def log_event(**entry) -> None:
    try:
        org_service().log_event(entry)
    except Exception:
        pass  # observability must never break the product path


def authorize_corpus(user: dict, org_id: str | None) -> None:
    """Access gate for a corpus: a business corpus needs membership
    plus an approved org; a personal corpus is the caller's own."""
    if org_id:
        org_role(user, org_id)  # any member may use the business corpus
        org_active(user, org_id)


def _openrag_search(query: str, filenames: list, limit: int = 40) -> list:
    """One OpenRAG chunk search, server-side filtered to the given
    document names. Returns raw chunk dicts (filename/text/score)."""
    resp = requests.post(
        f"{OPENRAG_URL}/search",
        json={"query": query, "filters": {"filename": filenames}, "limit": limit},
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"Search backend: {resp.text[:300]}"
        )
    data = resp.json()
    chunks = data if isinstance(data, list) else data.get("results", [])
    return [c for c in chunks if c.get("score", 0) >= MIN_SCORE]


def run_search(
    query: str,
    user: dict,
    page_size: int = 10,
    org_id: str | None = None,
    source: str = "web",
) -> dict:
    # Authorization happens here (membership + approval for orgs). The
    # corpus scope itself is enforced by filtering OpenRAG to exactly
    # this corpus's document names (each name carries its doc-id
    # prefix), plus a post-filter as a second line of defence.
    authorize_corpus(user, org_id)
    records = _corpus_records(user, org_id)
    by_name = {r["_openrag_name"]: r.get("filename", "") for r in records}
    started = time.monotonic()
    results = []
    if by_name:
        chunks = _openrag_search(query, sorted(by_name), limit=40)
        grouped: dict[str, dict] = {}
        order: list[str] = []
        for chunk in chunks:
            name = chunk.get("filename", "")
            if name not in by_name:
                continue  # another corpus's document - never returned
            if name not in grouped:
                grouped[name] = {
                    "title": by_name[name],
                    "link": "",
                    "snippets": [],
                    "extractive": [],
                    "_score": chunk.get("score", 0),
                }
                order.append(name)
            entry = grouped[name]
            entry["_score"] = max(entry["_score"], chunk.get("score", 0))
            text = clean_snippet(chunk.get("text", ""))
            if text and len(entry["snippets"]) < 3:
                entry["snippets"].append(text)
        order.sort(key=lambda n: -grouped[n]["_score"])
        for name in order[:page_size]:
            entry = grouped[name]
            entry.pop("_score", None)
            if not entry["snippets"]:
                # OpenRAG returns shell hits (filename, empty text) for
                # below-relevance matches; a result with no excerpt is
                # not a result. (Seen live 2026-11-03: an unrelated query
                # listed the caller's own docs with empty snippets.)
                continue
            results.append(entry)
    out = {"summary": "", "totalSize": len(results), "results": results}
    log_event(
        kind="search",
        uid=user["uid"],
        email=user.get("email", ""),
        orgId=org_id,
        query=query,
        summary="",
        results=out["totalSize"],
        latencyMs=int((time.monotonic() - started) * 1000),
        source=source,
    )
    return out


_files_cache: dict = {"at": 0.0, "names": {}}


def _openrag_files() -> dict:
    """All documents in OpenRAG: name -> {chunks, indexed_time}.
    Cached for a few seconds; list/search calls share one fetch."""
    now = time.monotonic()
    if now - _files_cache["at"] < 5:
        return _files_cache["names"]
    resp = requests.get(f"{OPENRAG_URL}/files", timeout=15)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"List backend: {resp.text[:300]}"
        )
    data = resp.json()
    entries = data.get("files", data) if isinstance(data, dict) else data
    names = {}
    for entry in entries or []:
        name = entry.get("filename") or entry.get("title") or ""
        if name:
            names[name] = entry
    _files_cache.update(at=now, names=names)
    return names


def _openrag_task_error(task_id: str) -> str | None:
    """Error message when an OpenRAG ingestion task failed; None when
    it succeeded, is still running, or cannot be checked."""
    if not task_id:
        return None
    for path in (f"/tasks/{task_id}", f"/v1/tasks/{task_id}"):
        try:
            resp = requests.get(f"{OPENRAG_URL}{path}", timeout=10)
        except Exception:
            return None
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            return None
        data = resp.json()
        status = str(data.get("status", "")).lower()
        if status in ("failed", "error", "failure"):
            return str(data.get("error") or data.get("message") or "ingestion failed")[:200]
        return None
    return None


def list_documents(user: dict, org_id: str | None = None) -> list:
    """Every file in this corpus, each with an ingestion status: a file
    appears the moment it is uploaded ('indexing'), flips to 'ready'
    once OpenRAG has indexed it, or shows 'failed' when its ingestion
    task reports an error."""
    authorize_corpus(user, org_id)
    records = _corpus_records(user, org_id)
    if not records:
        return []
    indexed = _openrag_files()
    out = {}
    for rec in records:
        doc_id = rec.get("docId", "")
        if rec.get("_openrag_name") in indexed:
            out[doc_id] = {
                "id": doc_id,
                "title": rec.get("filename") or doc_id,
                "status": "ready",
                "corpus": "global" if rec.get("_global") else "own",
            }
            continue
        error = _openrag_task_error(rec.get("openragTask", ""))
        item = {
            "id": doc_id,
            "title": rec.get("filename") or doc_id,
            "status": "failed" if error else "indexing",
            "corpus": "global" if rec.get("_global") else "own",
        }
        if error:
            item["error"] = error
        out[doc_id] = item
    return sorted(out.values(), key=lambda d: d["title"].lower())


class SearchBody(BaseModel):
    query: str
    orgId: str | None = None


@app.get("/api/config")
def config():
    try:
        backend = get_storage().backend
    except Exception:
        backend = "unknown"
    return {"auth": "khoj", "storage": backend}


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {
        "email": user.get("email"),
        "admin": is_super_admin(user),
        "superAdmin": is_super_admin(user),
    }


@app.post("/api/search")
def search(body: SearchBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return run_search(body.query, user, org_id=body.orgId)


class ChatBody(BaseModel):
    message: str
    contextId: str | None = None
    orgId: str | None = None


@app.post("/api/chat")
def chat(body: ChatBody, authorization: str | None = Header(default=None)):
    """Chat panel backend. The web service acts as an A2A client: it
    sends a JSON-RPC message/send to the Khoj A2A agent (AgentCard at
    <agent>/.well-known/agent-card.json) carrying the caller's token,
    and returns the agent's reply."""
    user = require_user(authorization)
    agent_url = os.environ.get("KHOJ_AGENT_URL", "http://khoj-agent")
    context_id = body.contextId or uuid.uuid4().hex
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "contextId": context_id,
                "parts": [{"kind": "text", "text": body.message}],
                "metadata": {"orgId": body.orgId} if body.orgId else {},
            }
        },
    }
    started = time.monotonic()
    try:
        resp = requests.post(
            agent_url + "/", json=rpc,
            headers={"Authorization": authorization or ""}, timeout=60,
        )
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chat agent unreachable: {e}")
    if "error" in data:
        raise HTTPException(
            status_code=502,
            detail=f"Chat agent: {data['error'].get('message', 'error')}",
        )
    result = data.get("result", {})
    text = " ".join(
        p.get("text", "") for p in result.get("parts", []) if p.get("kind") == "text"
    )
    sources = (result.get("metadata") or {}).get("sources", [])
    log_event(
        kind="chat",
        uid=user["uid"],
        email=user.get("email", ""),
        orgId=body.orgId,
        contextId=result.get("contextId") or context_id,
        query=body.message,
        summary=text[:500],
        answer=text,
        results=len(sources),
        latencyMs=int((time.monotonic() - started) * 1000),
        source="chat",
    )
    return {
        "reply": text,
        "contextId": result.get("contextId") or context_id,
        "sources": sources,
    }


@app.get("/api/chats")
def list_chats(authorization: str | None = Header(default=None)):
    """The caller's 5 most recent chat conversations, rebuilt from the
    chat event log so the sidebar can offer one-click reopen. Grouped by
    A2A contextId; events logged before context ids were recorded form
    single-exchange chats that reopen read-only."""
    user = require_user(authorization)
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for e in org_service().list_events(limit=1000):  # newest first
        if e.get("kind") != "chat" or e.get("uid") != user["uid"]:
            continue
        key = e.get("contextId") or f"single-{e.get('ts')}"
        chat = grouped.get(key)
        if chat is None:
            chat = grouped[key] = {
                "contextId": e.get("contextId"),
                "updatedAt": e.get("ts"),
                "pairs": [],
            }
            order.append(key)
        chat["pairs"].append(({"role": "user", "text": e.get("query", "")},
                               {"role": "assistant", "text": e.get("answer", "")}))
    chats = []
    for key in order[:5]:
        chat = grouped[key]
        # pairs were collected newest-first; flip pair order, keep each
        # pair's internal user->assistant order
        chat["messages"] = [m for pair in reversed(chat.pop("pairs")) for m in pair]
        first = next((m["text"] for m in chat["messages"] if m["role"] == "user"), "Chat")
        chat["title"] = (first or "Chat")[:60]
        chats.append(chat)
    return {"chats": chats}


SUGGESTIONS_FALLBACK = [
    "Summarize my documents",
    "What are the key points in my documents?",
    "How do I troubleshoot a problem using my documents?",
    "What do my documents say about getting started?",
]

# Generated suggestions are cached per user + document set: generation
# costs an LLM round trip, and the set only changes when documents do.
_SUGG_CACHE: dict = {}
_SUGG_TTL = 600


@app.get("/api/suggestions")
def suggestions(authorization: str | None = Header(default=None)):
    """Starter questions for an empty chat: the agent generates them
    from the caller's ready document titles. Any failure returns the
    static fallbacks — the empty state must always render."""
    user = require_user(authorization)
    titles = []
    for d in list_documents(user):
        if d.get("status") == "ready" and (d.get("title") or "").strip():
            titles.append(d["title"])
    cache_key = (user["uid"], tuple(titles[:8]))
    hit = _SUGG_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _SUGG_TTL:
        return {"suggestions": hit[1]}
    agent_url = os.environ.get("KHOJ_AGENT_URL", "http://khoj-agent")
    try:
        resp = requests.post(
            agent_url + "/suggestions",
            json={"documents": titles[:8]},
            headers={"Authorization": authorization or ""},
            timeout=30,
        )
        if resp.status_code == 200:
            out = [
                str(s).strip()
                for s in ((resp.json() or {}).get("suggestions") or [])
                if str(s).strip()
            ]
            if out:
                out = out[:4]
                _SUGG_CACHE[cache_key] = (time.time(), out)
                return {"suggestions": out}
    except Exception:
        pass
    return {"suggestions": list(SUGGESTIONS_FALLBACK)}


# --- Jira ticket creation ----------------------------------------------------
# After an answer, the chat UI offers "Create Jira ticket". The web service
# creates the issue on the configured Jira Cloud site (REST API v3, Basic
# auth: account email + API token). Config is env-only; the token arrives
# via External Secrets (GSM khoj-jira-api-token). Unconfigured = the status
# endpoint reports disabled and the UI hides the button.

def _jira_config() -> dict | None:
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    project = os.environ.get("JIRA_PROJECT_KEY", "")
    if not (base and email and token and project):
        return None

    def _names(env: str) -> list:
        return [p.strip() for p in os.environ.get(env, "").split(",") if p.strip()]

    return {
        "base": base,
        "email": email,
        "token": token,
        "project": project,
        "issue_type": os.environ.get("JIRA_ISSUE_TYPE", "Task"),
        # Some projects (company-managed ones especially) REQUIRE a
        # component and/or an affects-version on create. Optional env,
        # comma-separated names, sent only when configured.
        "components": _names("JIRA_COMPONENTS"),
        "versions": _names("JIRA_AFFECTS_VERSIONS"),
    }


def _adf(text: str) -> list:
    """Plain text -> Atlassian Document Format paragraph nodes."""
    nodes = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            nodes.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": line[:4000]}],
            })
    return nodes or [{"type": "paragraph", "content": [{"type": "text", "text": "-"}]}]


def _adf_heading(text: str) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": 3},
        "content": [{"type": "text", "text": text}],
    }


# Chat answers carry source citations like "[1]" / "[1], [4]" - they
# point at the chat's source list and are noise inside a ticket.
_CITE_RE = re.compile(r"(?:\s*\[\d+\])(?:\s*,\s*\[\d+\])*")
_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`)")


def _adf_inline(text: str) -> list:
    """One line of Markdown -> ADF text nodes with strong/em/code marks."""
    nodes = []
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "em"}]})
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        else:
            nodes.append({"type": "text", "text": part})
    return nodes or [{"type": "text", "text": "-"}]


def _md_to_adf(text: str) -> list:
    """Markdown-ish chat text -> ADF: headings, bullet/numbered lists,
    paragraphs with inline marks. Citations are stripped first."""
    text = _CITE_RE.sub("", text or "")
    nodes: list = []
    state = {"kind": None, "items": []}

    def flush():
        if state["kind"] and state["items"]:
            nodes.append({"type": state["kind"], "content": state["items"]})
        state["kind"], state["items"] = None, []

    for raw in text.splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^\s*[-*•]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if bullet or numbered:
            kind = "bulletList" if bullet else "orderedList"
            if state["kind"] != kind:
                flush()
                state["kind"] = kind
            state["items"].append({
                "type": "listItem",
                "content": [{"type": "paragraph", "content": _adf_inline((bullet or numbered).group(1))}],
            })
        elif heading:
            flush()
            nodes.append({
                "type": "heading",
                "attrs": {"level": min(6, len(heading.group(1)) + 2)},
                "content": _adf_inline(heading.group(2)),
            })
        elif not line.strip():
            flush()
        else:
            flush()
            nodes.append({"type": "paragraph", "content": _adf_inline(line.strip())})
    flush()
    return nodes or [{"type": "paragraph", "content": [{"type": "text", "text": "-"}]}]


@app.get("/api/jira/status")
def jira_status(authorization: str | None = Header(default=None)):
    require_user(authorization)
    cfg = _jira_config()
    if not cfg:
        return {"enabled": False}
    return {"enabled": True, "projectKey": cfg["project"], "baseUrl": cfg["base"]}


class JiraTicketBody(BaseModel):
    question: str
    answer: str
    orgId: str | None = None
    # Values the user picked when the UI prompted for the project's
    # required fields: {fieldKey: name} or {fieldKey: [names]}.
    extraFields: dict | None = None
    # The visible conversation ([{role: "user"|"agent", text}]): the
    # agent summarizes it into the ticket, and the full transcript is
    # appended so support sees everything the customer already tried.
    transcript: list | None = None


def _agent_summarize(authorization: str | None, transcript: list) -> str | None:
    """Ask the agent for an LLM summary of the conversation. Returns
    None on any failure — ticket creation must never depend on it."""
    agent_url = os.environ.get("KHOJ_AGENT_URL", "http://khoj-agent")
    try:
        resp = requests.post(
            agent_url + "/summarize",
            json={"transcript": transcript},
            headers={"Authorization": authorization or ""},
            timeout=45,
        )
        if resp.status_code != 200:
            return None
        summary = ((resp.json() or {}).get("summary") or "").strip()
        return summary or None
    except Exception:
        return None


def _transcript_adf(transcript: list) -> list:
    out = []
    for turn in transcript[-30:]:
        if not isinstance(turn, dict):
            continue
        text = " ".join(str(turn.get("text") or "").split())[:2000]
        if not text:
            continue
        who = "Customer" if turn.get("role") == "user" else "Assistant"
        out += _adf(f"{who}: {text}")
    return out


def _jira_user_account_id(cfg: dict, query: str) -> str:
    """Resolve a typed email/name to a Jira accountId.

    Cloud user fields (reporter, assignee) cannot be set by name or
    email in REST v3 - only by accountId - so a prompted value has to
    go through user search first. Exact email match wins.
    """
    try:
        resp = requests.get(
            f"{cfg['base']}/rest/api/3/user/search",
            params={"query": query},
            auth=(cfg["email"], cfg["token"]),
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Jira request failed: {e}")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Jira user lookup returned {resp.status_code}: {resp.text[:200]}",
        )
    users = resp.json() or []
    if not users:
        raise HTTPException(status_code=422, detail=f"No Jira user found for '{query}'")
    q = query.strip().lower()
    for u in users:
        if (u.get("emailAddress") or "").lower() == q:
            return u.get("accountId")
    return users[0].get("accountId")


@app.get("/api/jira/fields")
def jira_fields(authorization: str | None = Header(default=None)):
    """Required create-fields the ticket does NOT already cover.

    Reads the project's create metadata so the UI can prompt the user
    for exactly what this project demands (Components, Affects
    versions, ...) instead of the create failing with a 400. Fields
    Khoj fills itself, and fields covered by config
    (JIRA_COMPONENTS / JIRA_AFFECTS_VERSIONS), are never returned.
    """
    require_user(authorization)
    cfg = _jira_config()
    if not cfg:
        return {"enabled": False, "fields": []}
    try:
        resp = requests.get(
            f"{cfg['base']}/rest/api/3/issue/createmeta",
            params={
                "projectKeys": cfg["project"],
                "issuetypeNames": cfg["issue_type"],
                "expand": "projects.issuetypes.fields",
            },
            auth=(cfg["email"], cfg["token"]),
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Jira request failed: {e}")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Jira returned {resp.status_code}: {resp.text[:300]}",
        )
    projects = (resp.json() or {}).get("projects") or []
    fields_meta = {}
    if projects:
        types = projects[0].get("issuetypes") or []
        if types:
            fields_meta = types[0].get("fields") or {}
    covered = {"project", "issuetype", "summary", "description", "attachment"}
    if cfg["components"]:
        covered.add("components")
    if cfg["versions"]:
        covered.add("versions")
    out = []
    for key, meta in fields_meta.items():
        if key in covered or not meta.get("required"):
            continue
        allowed = [
            {"id": str(v.get("id", "")), "name": v.get("name") or v.get("value") or ""}
            for v in (meta.get("allowedValues") or [])
        ]
        out.append({
            "key": key,
            "name": meta.get("name") or key,
            "multi": (meta.get("schema") or {}).get("type") == "array",
            "allowedValues": allowed,
        })
    return {"enabled": True, "fields": out}


@app.post("/api/jira/ticket")
def jira_ticket(body: JiraTicketBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    cfg = _jira_config()
    if not cfg:
        raise HTTPException(status_code=501, detail="Jira is not configured on this deployment")
    question = body.question.strip()
    answer = body.answer.strip()
    if not question or not answer:
        raise HTTPException(status_code=422, detail="question and answer are required")
    summary = " ".join(question.split())
    if len(summary) > 200:
        summary = summary[:197] + "..."
    scope = "an organization corpus" if body.orgId else "personal documents"
    meta = f"Asked by {user.get('email', '')} via Customer Success Agent chat ({scope})."
    transcript = [t for t in (body.transcript or []) if isinstance(t, dict)]
    summary_md = _agent_summarize(authorization, transcript) if transcript else None
    description = []
    if summary_md:
        description += [_adf_heading("Conversation summary")] + _md_to_adf(summary_md)
    description += [_adf_heading("Question")] + _md_to_adf(question)
    description += [_adf_heading("Answer")] + _md_to_adf(answer)
    if summary_md:
        convo = _transcript_adf(transcript)
        if convo:
            description += [_adf_heading("Full conversation")] + convo
    description += _adf(meta)
    fields = {
            "project": {"key": cfg["project"]},
            "summary": summary,
            "issuetype": {"name": cfg["issue_type"]},
            "description": {
                "type": "doc",
                "version": 1,
                "content": description,
            },
    }
    if cfg["components"]:
        fields["components"] = [{"name": n} for n in cfg["components"]]
    if cfg["versions"]:
        fields["versions"] = [{"name": n} for n in cfg["versions"]]
    # User-prompted values for any other required fields (see
    # /api/jira/fields): lists become [{"name": ...}], a single value
    # becomes {"name": ...}. Core fields can never be overridden.
    for k, v in (body.extraFields or {}).items():
        if k in ("project", "issuetype", "summary", "description"):
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_]+", k or ""):
            continue
        if k in ("reporter", "assignee"):
            # User fields take an accountId, never a bare name/email.
            raw = v[0] if isinstance(v, list) and v else v
            if raw is not None and str(raw).strip():
                fields[k] = {"accountId": _jira_user_account_id(cfg, str(raw).strip())}
            continue
        if isinstance(v, list):
            names = [str(x).strip() for x in v if str(x).strip()]
            if names:
                fields[k] = [{"name": n} for n in names]
        elif v is not None and str(v).strip():
            fields[k] = {"name": str(v).strip()}
    payload = {"fields": fields}
    try:
        resp = requests.post(
            f"{cfg['base']}/rest/api/3/issue",
            json=payload,
            auth=(cfg["email"], cfg["token"]),
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Jira request failed: {e}")
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"Jira returned {resp.status_code}: {resp.text[:300]}",
        )
    data = resp.json()
    key = data.get("key", "")
    url = f"{cfg['base']}/browse/{key}" if key else cfg["base"]
    log_event(
        kind="jira",
        uid=user["uid"],
        email=user.get("email", ""),
        orgId=body.orgId,
        query=question,
        summary=f"Jira ticket {key}",
        results=None,
        latencyMs=None,
        source="chat",
    )
    return {"key": key, "url": url}


@app.get("/api/documents")
def documents(orgId: str | None = None, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {"documents": list_documents(user, org_id=orgId)}


def _scope_prefix_of(rec: dict, caller: dict) -> str:
    """Storage scope prefix for an upload record (its owner's prefix —
    for global-corpus records, the platform org's)."""
    key = rec.get("scopeKey", "")
    if key.startswith("org:"):
        return f"orgs/{key[4:]}"
    if key.startswith("user:"):
        return f"users/{key[5:]}"
    scope, _ = scope_of(caller, rec.get("orgId"))
    return scope


@app.get("/api/agent/corpus")
def agent_corpus(orgId: str | None = None, authorization: str | None = Header(default=None)):
    """The caller's searchable corpus for the A2A agent: authorization
    (membership + approval) is enforced here, and the agent receives
    only document descriptors — never scope assertions of its own.
    Replaces the agent's former direct database reads."""
    user = require_user(authorization)
    authorize_corpus(user, orgId)
    files = []
    for rec in _corpus_records(user, orgId):
        files.append({
            "docId": rec.get("docId", ""),
            "filename": rec.get("filename", ""),
            "openragName": rec.get("_openrag_name", ""),
            "scope": _scope_prefix_of(rec, user),
        })
    return {"files": files}


@app.get("/api/documents/{doc_id}/raw")
def document_raw(doc_id: str, authorization: str | None = Header(default=None)):
    """The stored original of one document. Access: the caller's own
    upload, an upload of a business they belong to, or the global
    corpus. Used by the chat agent's full-text handoff (small docs are
    read whole) — the agent holds no storage credentials of its own."""
    user = require_user(authorization)
    svc = org_service()
    record = None
    for rec in svc.list_all_uploads(limit=5000):
        if rec.get("docId") == doc_id:
            record = rec
            break
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    org_id = record.get("orgId")
    if org_id == PLATFORM_ORG_ID:
        pass  # global corpus: readable by every signed-in user
    elif org_id:
        org_role(user, org_id)
    elif record.get("uid") != user["uid"]:
        raise HTTPException(status_code=403, detail="Not your document")
    data = get_storage().get(
        f"{_scope_prefix_of(record, user)}/{record.get('filename', '')}"
    )
    if data is None:
        raise HTTPException(status_code=404, detail="Stored original not found")
    mime = _MIME_BY_EXT.get(
        os.path.splitext(record.get("filename", ""))[1].lower(),
        "application/octet-stream",
    )
    return Response(content=data, media_type=mime)


def _delete_knowledge_filter(openrag_name: str, filename: str) -> None:
    """Best-effort: remove the per-document OpenRAG knowledge filter
    created at upload time, so a deleted document also leaves OpenRAG's
    Knowledge tab. Matches the filter by its data_sources or by the
    exact 'Uploaded via Khoj' description. Never fails the delete."""
    display = os.path.splitext(filename)[0] or filename
    try:
        resp = requests.post(
            f"{OPENRAG_URL}/knowledge-filter/search",
            json={"query": display, "limit": 25},
            timeout=15,
        )
        if resp.status_code != 200:
            return
        candidates = []
        stack = [resp.json()]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if "id" in cur and ("queryData" in cur or "name" in cur):
                    candidates.append(cur)
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
        for entry in candidates:
            qd = entry.get("queryData") or entry.get("query_data") or {}
            sources = ((qd.get("filters") or {}).get("data_sources")) or []
            if openrag_name in sources or entry.get("description") == (
                f"Uploaded via Khoj: {filename}"
            ):
                try:
                    requests.delete(
                        f"{OPENRAG_URL}/knowledge-filter/{entry['id']}",
                        timeout=15,
                    )
                except Exception:
                    pass
    except Exception:
        pass


@app.delete("/api/documents/{doc_id}")
def delete_document(
    doc_id: str,
    orgId: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Delete one document from the caller's corpus (or, with orgId, a
    business corpus they admin — the same gate as upload). Removes the
    OpenRAG chunks, the stored original, the knowledge filter, and the
    upload record. The global corpus is only deletable from the
    platform org's own view, by its admins."""
    user = require_user(authorization)
    if orgId:
        org_role(user, orgId, allowed=UPLOAD_ROLES)
        org_active(user, orgId)
    scope, scope_key = scope_of(user, orgId)
    record = None
    for rec in org_service().list_uploads(scope_key):
        if rec.get("docId") == doc_id:
            record = rec
            break
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    filename = record.get("filename", "")
    or_name = openrag_filename(doc_id, filename)
    # OpenRAG reports "no matching chunks" as a failure (success:false,
    # deleted_chunks:0) — for a document that is still indexing or
    # failed, that IS the desired end state: nothing in the index.
    # Only a real index error blocks the delete.
    try:
        resp = requests.post(
            f"{OPENRAG_URL}/documents/delete-by-filename",
            json={"filename": or_name},
            timeout=30,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {}
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Index delete unreachable, nothing was removed: {exc}",
        )
    if not isinstance(payload, dict):
        payload = {}
    no_chunks = payload.get("deleted_chunks") == 0 and (
        "No matching document chunks" in str(payload.get("error") or "")
        or "No matching document chunks" in str(payload.get("message") or "")
    )
    index_ok = (resp.status_code == 200 and payload.get("success") is not False) or no_chunks
    if not index_ok:
        raise HTTPException(
            status_code=502,
            detail=f"Index delete failed, nothing was removed: {resp.text[:200]}",
        )
    _delete_knowledge_filter(or_name, filename)
    try:
        get_storage().delete(f"{scope}/{filename}")
    except Exception:
        pass
    org_service().remove_upload(doc_id)
    _files_cache["at"] = 0.0
    log_event(
        kind="delete",
        uid=user["uid"],
        email=user.get("email", ""),
        orgId=orgId,
        query=filename,
        results=None,
        latencyMs=None,
        source="web",
    )
    return {"deleted": doc_id, "title": filename}


# --- Businesses (organizations) ---------------------------------------------

class OrgBody(BaseModel):
    name: str


class MemberBody(BaseModel):
    email: str
    role: str = "member"


@app.post("/api/orgs")
def create_org(body: OrgBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Business name is required")
    return org_service().create_org(name, user)


@app.get("/api/orgs")
def my_orgs(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {"orgs": org_service().list_orgs_for_user(user["uid"])}


@app.get("/api/orgs/{org_id}/members")
def org_members(org_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    org_role(user, org_id)
    return {"members": org_service().list_members(org_id)}


@app.post("/api/orgs/{org_id}/members")
def add_org_member(org_id: str, body: MemberBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    org_role(user, org_id, allowed=UPLOAD_ROLES)
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="Role must be admin or member")
    target = _users.get_by_email(body.email.strip())
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="No Khoj account with that email - they must sign up first",
        )
    return org_service().add_member(
        org_id, {"uid": target["uid"], "email": target["email"]}, role=body.role
    )


@app.delete("/api/orgs/{org_id}/members/{uid}")
def remove_org_member(org_id: str, uid: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    org_role(user, org_id, allowed=UPLOAD_ROLES)
    svc = org_service()
    if svc.membership(uid, org_id) == "admin":
        admins = [m for m in svc.list_members(org_id) if m["role"] == "admin"]
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="A business must keep at least one admin")
    svc.remove_member(org_id, uid)
    return {"removed": uid}


# --- Invitations --------------------------------------------------------------

import secrets as _secrets


class InviteBody(BaseModel):
    email: str
    role: str = "member"


class AcceptBody(BaseModel):
    orgId: str
    inviteId: str
    secret: str | None = None


def _invite_secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@app.post("/api/orgs/{org_id}/invites")
def create_invite(
    org_id: str,
    body: InviteBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Admin invites an employee by email. Returns a one-time invite link
    the admin can share; the invitee can also accept in-app once they
    sign up with the invited email."""
    user = require_user(authorization)
    org_role(user, org_id, allowed=UPLOAD_ROLES)
    if body.role not in ROLES:
        raise HTTPException(status_code=422, detail="Role must be admin or member")
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="A valid email is required")
    secret = _secrets.token_urlsafe(24)
    invite = org_service().create_invite(
        org_id, email, body.role, user["uid"], _invite_secret_hash(secret)
    )
    base = str(request.base_url).rstrip("/")
    return {
        "inviteId": invite["id"],
        "email": email,
        "role": body.role,
        "inviteLink": f"{base}/?invite={org_id}.{invite['id']}.{secret}",
    }


@app.get("/api/orgs/{org_id}/invites")
def list_invites(org_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    org_role(user, org_id, allowed=UPLOAD_ROLES)
    return {"invites": org_service().list_invites(org_id)}


@app.delete("/api/orgs/{org_id}/invites/{invite_id}")
def revoke_invite(org_id: str, invite_id: str, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    org_role(user, org_id, allowed=UPLOAD_ROLES)
    invite = org_service().get_invite(org_id, invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    org_service().set_invite_status(org_id, invite_id, "revoked")
    return {"revoked": invite_id}


@app.get("/api/invites")
def my_invites(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {"invites": org_service().invites_for_email((user.get("email") or "").lower())}


@app.post("/api/invites/accept")
def accept_invite(body: AcceptBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    svc = org_service()
    invite = svc.get_invite(body.orgId, body.inviteId)
    if invite is None or invite.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Invite not found or no longer pending")
    email_match = (user.get("email") or "").lower() == (invite.get("email") or "").lower()
    secret_match = bool(body.secret) and _invite_secret_hash(body.secret) == invite.get("secretHash")
    if not (email_match or secret_match):
        raise HTTPException(status_code=403, detail="This invite belongs to a different email")
    member = svc.add_member(
        body.orgId,
        {"uid": user["uid"], "email": user.get("email", "")},
        role=invite.get("role", "member"),
    )
    svc.set_invite_status(body.orgId, body.inviteId, "accepted")
    org = svc.get_org(body.orgId)
    return {"joined": org, "role": member["role"]}


# --- Branding -------------------------------------------------------------------

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class BrandingBody(BaseModel):
    displayName: str | None = None
    logoUrl: str | None = None
    primaryColor: str | None = None
    tagline: str | None = None


@app.put("/api/orgs/{org_id}/branding")
def set_branding(org_id: str, body: BrandingBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    org_role(user, org_id, allowed=UPLOAD_ROLES)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "primaryColor" in updates and not _COLOR_RE.match(updates["primaryColor"]):
        raise HTTPException(status_code=422, detail="primaryColor must be a hex color like #4f46e5")
    if "logoUrl" in updates and updates["logoUrl"] and not updates["logoUrl"].startswith("https://"):
        raise HTTPException(status_code=422, detail="logoUrl must be an https URL")
    if len(updates.get("displayName", "")) > 60 or len(updates.get("tagline", "")) > 140:
        raise HTTPException(status_code=422, detail="Branding text is too long")
    svc = org_service()
    merged = {**svc.get_org(org_id).get("branding", {}), **updates}
    return {"branding": svc.set_branding(org_id, merged)}


@app.get("/api/public/org/{slug}")
def public_org(slug: str):
    """Public branding for an org's branded sign-in page (/?org=<slug>).
    Branding is public by design; no membership or document data here."""
    org = org_service().get_org_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Business not found")
    return {"name": org["name"], "slug": org["slug"], "branding": org.get("branding", {})}


_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".html": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
}


def _create_knowledge_filter(openrag_name: str, filename: str) -> None:
    """Best-effort: register an uploaded document as an OpenRAG
    knowledge filter so Khoj uploads also land in OpenRAG's Knowledge
    tab. That tab lists filters, not files; one filter per document
    (data_sources = [the document]) is OpenRAG's own onboarding
    convention. Never fails the upload."""
    display = os.path.splitext(filename)[0] or filename
    query_data = {
        "query": "",
        "filters": {
            "data_sources": [openrag_name],
            "document_types": ["*"],
            "owners": ["*"],
            "connector_types": ["*"],
        },
        "limit": 10,
        "scoreThreshold": 0,
        "color": "green",
        "icon": "file",
    }
    try:
        requests.post(
            f"{OPENRAG_URL}/knowledge-filter",
            json={
                "name": display,
                "description": f"Uploaded via Khoj: {filename}",
                "queryData": query_data,
            },
            timeout=15,
        )
    except Exception:
        pass


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    orgId: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
):
    """Upload into my corpus - or, with orgId, into a business corpus
    the caller owns or admins. The file goes to OpenRAG under its
    doc-id-prefixed name (the corpus boundary every search filter keys
    on); the original is also kept in the storage backend (local disk
    or GCS, per KHOJ_STORAGE) as the source of truth."""
    user = require_user(authorization)
    filename = os.path.basename(file.filename or "document")
    mime = file.content_type or _MIME_BY_EXT.get(
        os.path.splitext(filename)[1].lower(), "application/octet-stream"
    )
    if orgId:
        org_role(user, orgId, allowed=UPLOAD_ROLES)
        org_active(user, orgId)
    scope, scope_key = scope_of(user, orgId)
    data = file.file.read()

    get_storage().put(f"{scope}/{filename}", data, mime)

    doc_id = document_id(scope, filename)
    or_name = openrag_filename(doc_id, filename)
    resp = requests.post(
        f"{OPENRAG_URL}/router/upload_ingest",
        files={"file": (or_name, io.BytesIO(data), mime)},
        timeout=120,
    )
    if resp.status_code not in (200, 202):
        raise HTTPException(
            status_code=502, detail=f"Ingestion trigger: {resp.text[:300]}"
        )
    task_id = ""
    try:
        task_id = resp.json().get("task_id", "")
    except Exception:
        pass
    try:
        org_service().record_upload({
            "docId": doc_id,
            "scopeKey": scope_key,
            "uid": user["uid"],
            "orgId": orgId,
            "filename": filename,
            "openragTask": task_id,
            "uploadedAt": int(time.time()),
        })
    except Exception:
        pass  # status tracking is best-effort; the upload itself succeeded
    _create_knowledge_filter(or_name, filename)
    log_event(
        kind="upload",
        uid=user["uid"],
        email=user.get("email", ""),
        orgId=orgId,
        query=filename,
        results=None,
        latencyMs=None,
        source="web",
    )
    return JSONResponse(
        {
            "uploaded": filename,
            "documentId": doc_id,
            "task": task_id,
            "at": int(time.time()),
        }
    )


# Backward-compatible alias for the first UI revision (admin upload).
app.post("/api/admin/upload")(upload)


# --- Super admin (platform org) -------------------------------------------------

class DecisionBody(BaseModel):
    decision: str  # "approve" | "reject"


def _org_name_map(svc) -> dict:
    names = {PLATFORM_ORG_ID: "Khoj (global corpus)"}
    for status in ("approved", "pending", "rejected"):
        for o in svc.list_orgs_by_status(status):
            names[o["id"]] = o.get("name", o["id"])
    return names


@app.get("/api/admin/org-requests")
def org_requests(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_super_admin(user)
    return {"orgs": org_service().list_orgs_by_status("pending")}


@app.post("/api/admin/orgs/{org_id}/decision")
def org_decision(org_id: str, body: DecisionBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_super_admin(user)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="decision must be approve or reject")
    svc = org_service()
    if svc.get_org(org_id) is None:
        raise HTTPException(status_code=404, detail="Business not found")
    svc.set_org_status(org_id, "approved" if body.decision == "approve" else "rejected")
    return {"orgId": org_id, "status": svc.get_org(org_id)["status"]}


@app.get("/api/admin/logs")
def admin_logs(limit: int = 100, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    require_super_admin(user)
    return {"logs": org_service().list_events(limit=min(limit, 500))}


@app.get("/api/admin/conversations")
def admin_conversations(limit: int = 100, authorization: str | None = Header(default=None)):
    """Every user's questions with the answers they got (chat + search),
    newest first — the super admin's inspection view."""
    user = require_user(authorization)
    require_super_admin(user)
    svc = org_service()
    names = _org_name_map(svc)
    out = []
    for e in svc.list_events(limit=500):
        if e.get("kind") not in ("chat", "search"):
            continue
        out.append({
            "ts": e.get("ts"),
            "email": e.get("email", ""),
            "kind": e.get("kind"),
            "orgId": e.get("orgId"),
            "orgName": names.get(e.get("orgId") or "", "Personal") if e.get("orgId") else "Personal",
            "question": e.get("query", ""),
            "answer": e.get("answer") or e.get("summary") or "",
            "results": e.get("results"),
            "latencyMs": e.get("latencyMs"),
        })
        if len(out) >= min(limit, 200):
            break
    return {"conversations": out}


@app.get("/api/admin/uploads")
def admin_uploads(authorization: str | None = Header(default=None)):
    """All uploaded files, grouped: the global corpus (platform org),
    then per business, then personal uploads by user."""
    user = require_user(authorization)
    require_super_admin(user)
    svc = org_service()
    names = _org_name_map(svc)
    try:
        indexed = _openrag_files()
    except Exception:
        indexed = {}
    groups_global, groups_orgs, groups_personal = [], {}, {}
    for rec in svc.list_all_uploads():
        entry = {
            "filename": rec.get("filename", ""),
            "uploadedAt": rec.get("uploadedAt"),
            "uid": rec.get("uid", ""),
            "status": "ready" if openrag_filename(rec.get("docId", ""), rec.get("filename", "")) in indexed else "indexing",
        }
        org_id = rec.get("orgId")
        if org_id == PLATFORM_ORG_ID:
            groups_global.append(entry)
        elif org_id:
            groups_orgs.setdefault(org_id, []).append(entry)
        else:
            groups_personal.setdefault(rec.get("uid", ""), []).append(entry)
    newest = lambda files: sorted(files, key=lambda f: -(f.get("uploadedAt") or 0))
    return {
        "global": newest(groups_global),
        "orgs": [
            {"orgId": oid, "orgName": names.get(oid, oid), "files": newest(files)}
            for oid, files in sorted(groups_orgs.items(), key=lambda kv: names.get(kv[0], kv[0]))
        ],
        "personal": [
            {"uid": uid, "files": newest(files)}
            for uid, files in sorted(groups_personal.items())
        ],
    }


def _parse_ts(value):
    """Best-effort epoch-seconds from a number, numeric string, or ISO
    timestamp; None when unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    s = str(value).strip()
    try:
        f = float(s)
        return f / 1000.0 if f > 1e12 else f
    except ValueError:
        pass
    try:
        import datetime as _dt
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return None


@app.get("/api/admin/file-stats")
def admin_file_stats(authorization: str | None = Header(default=None)):
    """The ingestion matrix: per file — corpus, chunk count, and how long
    ingestion took (upload time -> indexed time), grouped global / org /
    personal like /api/admin/uploads."""
    user = require_user(authorization)
    require_super_admin(user)
    svc = org_service()
    names = _org_name_map(svc)
    try:
        indexed = _openrag_files()
    except Exception:
        indexed = {}

    def entry(rec):
        meta = indexed.get(openrag_filename(rec.get("docId", ""), rec.get("filename", "")), {})
        up_ts = _parse_ts(rec.get("uploadedAt"))
        idx_ts = _parse_ts(meta.get("indexed_time"))
        ingest = None
        if up_ts and idx_ts and idx_ts >= up_ts:
            ingest = round(idx_ts - up_ts, 1)
        return {
            "filename": rec.get("filename", ""),
            "uploadedAt": rec.get("uploadedAt"),
            "uid": rec.get("uid", ""),
            "chunks": meta.get("chunks"),
            "ingestionSec": ingest,
            "status": "ready" if meta else "indexing",
        }

    groups_global, groups_orgs, groups_personal = [], {}, {}
    for rec in svc.list_all_uploads():
        e = entry(rec)
        org_id = rec.get("orgId")
        if org_id == PLATFORM_ORG_ID:
            groups_global.append(e)
        elif org_id:
            groups_orgs.setdefault(org_id, []).append(e)
        else:
            groups_personal.setdefault(rec.get("uid", ""), []).append(e)
    newest = lambda files: sorted(files, key=lambda f: -(f.get("uploadedAt") or 0))
    return {
        "global": newest(groups_global),
        "orgs": [
            {"orgId": oid, "orgName": names.get(oid, oid), "files": newest(files)}
            for oid, files in sorted(groups_orgs.items(), key=lambda kv: names.get(kv[0], kv[0]))
        ],
        "personal": [
            {"uid": uid, "files": newest(files)}
            for uid, files in sorted(groups_personal.items())
        ],
    }


@app.get("/api/admin/metrics")
def admin_metrics(authorization: str | None = Header(default=None)):
    """How the RAG is performing, computed from the search log."""
    user = require_user(authorization)
    require_super_admin(user)
    svc = org_service()
    events = svc.list_events(limit=1000)
    searches = [e for e in events if e.get("kind") == "search"]
    uploads = [e for e in events if e.get("kind") == "upload"]
    now = time.time()
    last7 = [e for e in searches if now - e.get("ts", 0) <= 7 * 86400]
    lat = [e["latencyMs"] for e in searches if e.get("latencyMs") is not None]
    zero = [e for e in searches if e.get("results") == 0]
    per_org: dict = {}
    for e in searches:
        key = e.get("orgId") or "personal"
        per_org[key] = per_org.get(key, 0) + 1
    top = sorted(
        ({"orgId": k, "searches": v} for k, v in per_org.items()),
        key=lambda x: -x["searches"],
    )[:10]
    return {
        "orgs": {
            "pending": len(svc.list_orgs_by_status("pending")),
            "approved": len(svc.list_orgs_by_status("approved")),
            "rejected": len(svc.list_orgs_by_status("rejected")),
        },
        "searches": {
            "total": len(searches),
            "last7d": len(last7),
            "uploads": len(uploads),
            "uniqueUsers": len({e.get("uid") for e in searches}),
            "avgLatencyMs": int(sum(lat) / len(lat)) if lat else None,
            "zeroResultRate": round(len(zero) / len(searches), 3) if searches else None,
            "viaMcp": len([e for e in searches if e.get("source") == "mcp"]),
            "viaWeb": len([e for e in searches if e.get("source") == "web"]),
        },
        "topCorpora": top,
    }


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )
    try:
        orgs = org_service()
    except Exception:
        orgs = None  # businesses unavailable; personal tools still work

    def mcp_search(query, u, page_size=10, org_id=None):
        return run_search(query, u, page_size, org_id=org_id, source="mcp")

    response = handle_rpc(body, user, mcp_search, list_documents, orgs=orgs)
    if response is None:  # notification: accepted, no body
        return JSONResponse(status_code=202, content={})
    if "error" in response and response["error"].get("code") == -32601:
        return JSONResponse(response, status_code=404)
    return JSONResponse(response)


@app.get("/mcp")
def mcp_get():
    # Stateless server: no server-initiated SSE stream.
    raise HTTPException(status_code=405, detail="Use POST /mcp")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
