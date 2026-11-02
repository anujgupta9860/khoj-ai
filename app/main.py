"""Khoj.ai backend.

Product model: every signed-in user uploads their OWN documents and can
search only their own corpus. Isolation is enforced server-side: each
document is imported with structData {"user_id": <firebase uid>} and every
search carries the filter user_id: ANY("<uid from the verified token>") -
the uid never comes from client input.

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
import time

import requests
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import firebase_admin
from firebase_admin import auth as fb_auth
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage

from .mcp_server import handle_rpc

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


def run_search(query: str, user: dict, page_size: int = 10) -> dict:
    url = f"{DE_BASE}/engines/{ENGINE_ID}/servingConfigs/default_search:search"
    payload = {
        "query": query,
        "pageSize": page_size,
        "filter": user_filter(user),
        "spellCorrectionSpec": {"mode": "AUTO"},
        "queryExpansionSpec": {"condition": "AUTO"},
        "contentSearchSpec": {
            "summarySpec": {
                "summaryResultCount": 3,
                "includeCitations": True,
                "ignoreAdversarialQuery": True,
            },
            "snippetSpec": {"returnSnippet": True},
            "extractiveContentSpec": {"maxExtractiveAnswerCount": 1},
        },
    }
    resp = requests.post(url, json=payload, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search backend: {resp.text[:300]}")
    data = resp.json()
    results = []
    for r in data.get("results", []):
        doc = r.get("document", {})
        derived = doc.get("derivedStructData", {})
        struct = doc.get("structData", {})
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
    return {
        "summary": data.get("summary", {}).get("summaryText", ""),
        "totalSize": data.get("totalSize", len(results)),
        "results": results,
    }


def list_documents(user: dict) -> list:
    url = f"{DE_BASE}/dataStores/{DATASTORE_ID}/branches/0/documents"
    mine, token = [], None
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
            if struct.get("user_id") == user["uid"]:
                mine.append({
                    "id": d.get("id", ""),
                    "title": struct.get("title") or d.get("id", ""),
                })
        token = data.get("nextPageToken")
        if not token:
            break
    return mine


class SearchBody(BaseModel):
    query: str


@app.get("/api/config")
def config():
    return FIREBASE_WEB_CONFIG


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {"email": user.get("email"), "admin": bool(user.get("admin"))}


@app.post("/api/search")
def search(body: SearchBody, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return run_search(body.query, user)


@app.get("/api/documents")
def documents(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    return {"documents": list_documents(user)}


_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".html": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
}


def _import_document(user: dict, filename: str, content_type: str | None):
    """Upload bytes under the user's prefix + a custom-schema JSONL record
    carrying structData.user_id, then import just that record."""
    uid = user["uid"]
    mime = content_type or _MIME_BY_EXT.get(
        os.path.splitext(filename)[1].lower(), "application/octet-stream"
    )
    client = storage.Client(project=PROJECT)
    bucket = client.bucket(BUCKET)
    object_name = f"users/{uid}/{filename}"
    blob = bucket.blob(object_name)
    return mime, bucket, object_name, blob


@app.post("/api/upload")
def upload(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = require_user(authorization)
    filename = os.path.basename(file.filename or "document")
    mime, bucket, object_name, blob = _import_document(user, filename, file.content_type)
    blob.upload_from_file(file.file, content_type=mime)

    doc_id = "doc-" + hashlib.sha256(f"{user['uid']}/{filename}".encode()).hexdigest()[:24]
    record = {
        "id": doc_id,
        "structData": {
            "user_id": user["uid"],
            "owner_email": user.get("email", ""),
            "title": filename,
        },
        "content": {"mimeType": mime, "uri": f"gs://{BUCKET}/{object_name}"},
    }
    meta = bucket.blob(f"meta/{user['uid']}/{filename}.jsonl")
    meta.upload_from_string(json.dumps(record) + "\n", content_type="application/json")

    import_url = f"{DE_BASE}/dataStores/{DATASTORE_ID}/branches/0/documents:import"
    payload = {
        "gcsSource": {
            "inputUris": [f"gs://{BUCKET}/meta/{user['uid']}/{filename}.jsonl"],
            "dataSchema": "custom",
        },
        "reconciliationMode": "INCREMENTAL",
    }
    resp = requests.post(import_url, json=payload, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Import trigger: {resp.text[:300]}")
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
    response = handle_rpc(body, user, run_search, list_documents)
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
