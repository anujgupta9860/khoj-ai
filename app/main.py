"""Khoj.ai backend.

Product model: every signed-in user uploads their OWN documents and can
search only their own corpus. Businesses (orgs, app/orgs.py) are the
second flavor: a business has N member users who share one corpus.
Isolation is enforced server-side: documents are imported with
structData {"user_id": <uid>} or {"org_id": <orgId>}, and every search
carries a filter built only from the verified token (+ a Firestore
membership check for org corpora) - scope ids never come from raw
client assertions.

Surfaces:
  Web UI + API (static app in app/static):
    GET  /api/config       Firebase web config (public identifiers)
    GET  /api/me           Who am I (+ admin claim, kept for future use)
    POST /api/search       Search my documents (Vertex AI Search)
    GET  /api/documents    List my documents
    POST /api/upload       Upload a document into my corpus
  MCP server (app/mcp_server.py) for the user's own agent:
    POST /mcp              Streamable-HTTP MCP endpoint (Bearer ID token);
                           tools: search_documents, list_my_documents

All GCP access uses Application Default Credentials (Workload Identity on
GKE) - no keys in code or repo.
"""

import hashlib
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
ENGINE_ID = os.environ["KHOJ_ENGINE_ID"]
DATASTORE_ID = os.environ["KHOJ_DATASTORE_ID"]
BUCKET = os.environ["KHOJ_BUCKET"]
LOCATION = os.environ.get("KHOJ_LOCATION", "global")

FIREBASE_WEB_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
    "projectId": PROJECT,
}

firebase_admin.initialize_app()

app = FastAPI(title="Khoj.ai")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DE_BASE = (
    f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT}"
    f"/locations/{LOCATION}/collections/default_collection"
)


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing ID token")
    try:
        return fb_auth.verify_id_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid ID token")


def gcp_access_token() -> str:
    creds, _ = google_auth_default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


def _headers() -> dict:
    return {"Authorization": f"Bearer {gcp_access_token()}"}


def user_filter(user: dict) -> str:
    # The isolation boundary. uid comes from the verified Firebase token.
    return f'user_id: ANY("{user["uid"]}")'


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


def corpus_filter(user: dict, org_id: str | None) -> str:
    if org_id:
        org_role(user, org_id)  # any member may search the business corpus
        org_active(user, org_id)
        return f'org_id: ANY("{org_id}")'
    return user_filter(user)


def run_search(
    query: str,
    user: dict,
    page_size: int = 10,
    org_id: str | None = None,
    source: str = "web",
) -> dict:
    # Authorization happens here (membership + approval for orgs); the
    # corpus scope itself is enforced by post-filtering below, because
    # this data store's schema (auto-derived from the first, untagged
    # seed import) does not know user_id/org_id as filterable fields -
    # Vertex rejects a filter on them with "Unsupported field".
    corpus_filter(user, org_id)
    key, want = ("org_id", org_id) if org_id else ("user_id", user["uid"])
    url = f"{DE_BASE}/engines/{ENGINE_ID}/servingConfigs/default_search:search"
    base = {
        "query": query,
        "pageSize": min(page_size * 5, 50),
        "spellCorrectionSpec": {"mode": "AUTO"},
        "queryExpansionSpec": {"condition": "AUTO"},
    }
    rich_spec = {
        "summarySpec": {
            "summaryResultCount": 3,
            "includeCitations": True,
            "ignoreAdversarialQuery": True,
        },
        "snippetSpec": {"returnSnippet": True},
        "extractiveContentSpec": {"maxExtractiveAnswerCount": 1},
    }
    # Summary + extractive answers are Enterprise-edition features. The
    # engine may be Standard edition (cheaper); try the rich request and
    # fall back to snippet-only search when Vertex rejects it, instead of
    # failing the user's search.
    started = time.monotonic()
    resp = requests.post(
        url, json={**base, "contentSearchSpec": rich_spec},
        headers=_headers(), timeout=30,
    )
    if resp.status_code == 400 and "enterprise edition" in resp.text.lower():
        resp = requests.post(
            url,
            json={**base, "contentSearchSpec": {"snippetSpec": {"returnSnippet": True}}},
            headers=_headers(), timeout=30,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search backend: {resp.text[:300]}")
    data = resp.json()
    results = []
    for r in data.get("results", []):
        doc = r.get("document", {})
        derived = doc.get("derivedStructData", {})
        struct = doc.get("structData", {})
        if struct.get(key) != want:
            continue  # another corpus's document - never returned
        if len(results) >= page_size:
            break
        results.append(
            {
                "title": struct.get("title") or derived.get("title") or doc.get("id", ""),
                "link": derived.get("link", ""),
                "snippets": [
                    s.get("snippet", "")
                    for s in derived.get("snippets", [])
                    if s.get("snippet")
                ],
                "extractive": [
                    s.get("content", "")
                    for s in derived.get("extractive_answers", [])
                    if s.get("content")
                ],
            }
        )
    out = {
        "summary": data.get("summary", {}).get("summaryText", ""),
        "totalSize": len(results),
        "results": results,
    }
    log_event(
        kind="search",
        uid=user["uid"],
        email=user.get("email", ""),
        orgId=org_id,
        query=query,
        summary=out["summary"][:500],
        results=out["totalSize"],
        latencyMs=int((time.monotonic() - started) * 1000),
        source=source,
    )
    return out


def _indexed_documents(user: dict, org_id: str | None = None) -> dict:
    """Documents already present in the search index for this corpus."""
    if org_id:
        org_role(user, org_id)
        org_active(user, org_id)
        key, want = "org_id", org_id
    else:
        key, want = "user_id", user["uid"]
    url = f"{DE_BASE}/dataStores/{DATASTORE_ID}/branches/0/documents"
    mine, token = {}, None
    for _ in range(5):  # cap the scan at POC scale
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        resp = requests.get(url, params=params, headers=_headers(), timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"List backend: {resp.text[:300]}")
        data = resp.json()
        for d in data.get("documents", []):
            struct = d.get("structData", {})
            if struct.get(key) == want:
                mine[d.get("id", "")] = struct.get("title") or d.get("id", "")
        token = data.get("nextPageToken")
        if not token:
            break
    return mine


def _operation_error(operation: str) -> str | None:
    """Error message when a Vertex import operation finished with an
    error; None when it is still running, finished cleanly, or cannot
    be checked."""
    if not operation:
        return None
    try:
        resp = requests.get(
            f"https://discoveryengine.googleapis.com/v1/{operation}",
            headers=_headers(), timeout=15,
        )
        if resp.status_code != 200:
            return None
        err = resp.json().get("error")
        if err:
            return str(err.get("message", "import failed"))[:200]
    except Exception:
        pass
    return None


def list_documents(user: dict, org_id: str | None = None) -> list:
    """Every file in this corpus, each with an ingestion status: a file
    appears the moment it is uploaded ('indexing'), flips to 'ready'
    once the search index contains it, or shows 'failed' when its
    import operation reports an error."""
    indexed = _indexed_documents(user, org_id)
    scope_key = f"org:{org_id}" if org_id else f"user:{user['uid']}"
    try:
        records = org_service().list_uploads(scope_key)
    except Exception:
        records = []
    out = {}
    for rec in records:
        doc_id = rec.get("docId", "")
        if doc_id in indexed:
            out[doc_id] = {
                "id": doc_id,
                "title": rec.get("filename") or indexed[doc_id],
                "status": "ready",
            }
            continue
        error = _operation_error(rec.get("operation", ""))
        item = {
            "id": doc_id,
            "title": rec.get("filename") or doc_id,
            "status": "failed" if error else "indexing",
        }
        if error:
            item["error"] = error
        out[doc_id] = item
    for doc_id, title in indexed.items():
        out.setdefault(doc_id, {"id": doc_id, "title": title, "status": "ready"})
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
    """Upload into my corpus - or, with orgId, into a business corpus the
    caller owns or admins. The structData scope key (user_id / org_id) is
    what every search filter keys on."""
    user = require_user(authorization)
    filename = os.path.basename(file.filename or "document")
    mime = file.content_type or _MIME_BY_EXT.get(
        os.path.splitext(filename)[1].lower(), "application/octet-stream"
    )
    if orgId:
        org_role(user, orgId, allowed=UPLOAD_ROLES)
        org_active(user, orgId)
        scope = f"orgs/{orgId}"
        struct = {"org_id": orgId, "uploaded_by": user["uid"], "title": filename}
    else:
        scope = f"users/{user['uid']}"
        struct = {
            "user_id": user["uid"],
            "owner_email": user.get("email", ""),
            "title": filename,
        }
    bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    object_name = f"{scope}/{filename}"
    bucket.blob(object_name).upload_from_file(file.file, content_type=mime)

    doc_id = "doc-" + hashlib.sha256(f"{scope}/{filename}".encode()).hexdigest()[:24]
    record = {
        "id": doc_id,
        "structData": struct,
        "content": {"mimeType": mime, "uri": f"gs://{BUCKET}/{object_name}"},
    }
    meta_name = f"meta/{scope}/{filename}.jsonl"
    bucket.blob(meta_name).upload_from_string(
        json.dumps(record) + "\n", content_type="application/json"
    )

    import_url = f"{DE_BASE}/dataStores/{DATASTORE_ID}/branches/0/documents:import"
    payload = {
        "gcsSource": {
            "inputUris": [f"gs://{BUCKET}/{meta_name}"],
            "dataSchema": "custom",
        },
        "reconciliationMode": "INCREMENTAL",
    }
    resp = requests.post(import_url, json=payload, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Import trigger: {resp.text[:300]}")
    try:
        org_service().record_upload({
            "docId": doc_id,
            "scopeKey": f"org:{orgId}" if orgId else f"user:{user['uid']}",
            "uid": user["uid"],
            "orgId": orgId,
            "filename": filename,
            "operation": resp.json().get("name", ""),
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
            "import": resp.json().get("name", ""),
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
