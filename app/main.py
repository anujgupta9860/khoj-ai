"""Khoj.ai backend.

Serves the static UI and three API endpoints:
  GET  /api/config         Firebase web config (public identifiers) for the UI
  POST /api/search         Verify Firebase ID token, then query Vertex AI Search
  POST /api/admin/upload   Admin-only: upload a document to the corpus bucket
                           and trigger a Vertex AI Search import

All GCP access uses Application Default Credentials (on the GCE VM this is
the VM's service account via the metadata server) - no keys in code or repo.
"""

import os
import time

import requests
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import firebase_admin
from firebase_admin import auth as fb_auth
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage

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


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing ID token")
    try:
        return fb_auth.verify_id_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid ID token")


def require_admin(authorization: str | None) -> dict:
    user = require_user(authorization)
    if not user.get("admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def gcp_access_token() -> str:
    creds, _ = google_auth_default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


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
    require_user(authorization)
    url = (
        f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT}"
        f"/locations/{LOCATION}/collections/default_collection"
        f"/engines/{ENGINE_ID}/servingConfigs/default_search:search"
    )
    payload = {
        "query": body.query,
        "pageSize": 10,
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
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {gcp_access_token()}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Search backend: {resp.text[:300]}")
    data = resp.json()
    results = []
    for r in data.get("results", []):
        doc = r.get("document", {})
        derived = doc.get("derivedStructData", {})
        results.append(
            {
                "title": derived.get("title") or doc.get("id", ""),
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


@app.post("/api/admin/upload")
def upload(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    blob_name = file.filename
    client = storage.Client(project=PROJECT)
    bucket = client.bucket(BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_file(file.file, content_type=file.content_type)
    # Import the whole corpus prefix (incremental import is cheap at POC scale
    # and idempotent: documents are keyed by their GCS URI).
    import_url = (
        f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT}"
        f"/locations/{LOCATION}/collections/default_collection"
        f"/dataStores/{DATASTORE_ID}/branches/0/documents:import"
    )
    payload = {
        "gcsSource": {"inputUris": [f"gs://{BUCKET}/*"], "dataSchema": "content"},
        "reconciliationMode": "INCREMENTAL",
    }
    resp = requests.post(
        import_url,
        json=payload,
        headers={"Authorization": f"Bearer {gcp_access_token()}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Import trigger: {resp.text[:300]}")
    return JSONResponse(
        {"uploaded": blob_name, "import": resp.json().get("name", ""), "at": int(time.time())}
    )


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
