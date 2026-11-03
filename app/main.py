"""Khoj.ai backend.

Product model: every signed-in user uploads their OWN documents and can
search only their own corpus. Businesses (orgs, app/orgs.py) are the
second flavor: a business has N member users who share one corpus.
Isolation is enforced server-side: OpenRAG is the single retrieval
engine, and every document Khoj sends it is stored under a unique name
derived from the scope + filename ("<docId>--<filename>"). Every search
is filtered to exactly the caller's document names, built only from the
verified token (+ a Firestore membership check for org corpora) - scope
ids never come from raw client assertions. The original file is also
kept in GCS as a cold copy (source of truth for re-ingestion).

Surfaces:
  Web UI + API (static app in app/static):
    GET  /api/config       Firebase web config (public identifiers)
    GET  /api/me           Who am I (+ admin claim, kept for future use)
    POST /api/search       Search my documents (OpenRAG)
    GET  /api/documents    List my documents
    POST /api/upload       Upload a document into my corpus
  MCP server (app/mcp_server.py) for the user's own agent:
    POST /mcp              Streamable-HTTP MCP endpoint (Bearer ID token);
                           tools: search_documents, list_my_documents

All GCP access uses Application Default Credentials (Workload Identity on
GKE) - no keys in code or repo.
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
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import firebase_admin
from firebase_admin import auth as fb_auth
from firebase_admin import firestore as fb_firestore
from fastapi import Form
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage

from .mcp_server import handle_rpc
from .orgs import PLATFORM_ORG_ID, ROLES, UPLOAD_ROLES, FirestoreOrgService

PROJECT = os.environ["GCP_PROJECT"]
BUCKET = os.environ["KHOJ_BUCKET"]
# OpenRAG is the single retrieval engine (in-cluster service).
OPENRAG_URL = os.environ.get("OPENRAG_URL", "http://openrag-backend:8000")
# NOTE: the OpenRAG backend serves UNPREFIXED routes (/search, /files,
# /router/upload_ingest, /tasks/...). The /api/* prefix exists only on the
# OpenRAG frontend's proxy (frontend/app/api/[...path] strips it). Calling
# the backend with /api/* returns 404 - learned live 2026-11-03.

FIREBASE_WEB_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
    "projectId": PROJECT,
}

firebase_admin.initialize_app()

app = FastAPI(title="Khoj.ai")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


_TAG_RE = re.compile(r"<[^>]+>")


def clean_snippet(text: str) -> str:
    """Search snippets can carry highlight markup (<b>...</b>) and HTML
    entities (&nbsp;). Strip both so results read as plain prose."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing ID token")
    try:
        return fb_auth.verify_id_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid ID token")


def document_id(scope: str, filename: str) -> str:
    """Stable per-(scope, filename) document id."""
    return "doc-" + hashlib.sha256(f"{scope}/{filename}".encode()).hexdigest()[:24]


def openrag_filename(doc_id: str, filename: str) -> str:
    """The name a document carries inside OpenRAG. The doc-id prefix
    makes it unique per scope, so an OpenRAG filename filter is an
    exact corpus boundary (two users can both upload 'resume.pdf')."""
    return f"{doc_id}--{filename}"


def scope_of(user: dict, org_id: str | None) -> tuple[str, str]:
    """(GCS scope prefix, Firestore scope key) for a corpus. Org
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
        _org_service = FirestoreOrgService(fb_firestore.client())
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
    if isinstance(data, list):
        return data
    return data.get("results", [])


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
    return FIREBASE_WEB_CONFIG


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {
        "email": user.get("email"),
        "admin": bool(user.get("admin")),
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
    <agent>/.well-known/agent-card.json) carrying the caller's Firebase
    token, and returns the agent's reply."""
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
        query=body.message,
        summary=text[:500],
        results=len(sources),
        latencyMs=int((time.monotonic() - started) * 1000),
        source="chat",
    )
    return {
        "reply": text,
        "contextId": result.get("contextId") or context_id,
        "sources": sources,
    }


@app.get("/api/documents")
def documents(orgId: str | None = None, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {"documents": list_documents(user, org_id=orgId)}


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
    try:
        target = fb_auth.get_user_by_email(body.email.strip())
    except Exception:
        raise HTTPException(
            status_code=404,
            detail="No Khoj account with that email - they must sign up first",
        )
    return org_service().add_member(
        org_id, {"uid": target.uid, "email": target.email}, role=body.role
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


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    orgId: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
):
    """Upload into my corpus - or, with orgId, into a business corpus
    the caller owns or admins. The file goes to OpenRAG under its
    doc-id-prefixed name (the corpus boundary every search filter keys
    on); the original is also kept in GCS as a cold copy."""
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

    bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    bucket.blob(f"{scope}/{filename}").upload_from_string(data, content_type=mime)

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
