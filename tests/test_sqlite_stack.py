"""Tests for the post-Firebase stack: built-in auth (app/auth.py),
the SQLite org service, the storage backend switch, and a full
register -> upload -> raw-fetch flow through the real API with real
token verification (only OpenRAG HTTP is stubbed).

Run:  python tests/test_sqlite_stack.py
"""

import io
import os
import sys
import tempfile

os.environ["KHOJ_DATA_DIR"] = tempfile.mkdtemp(prefix="khoj-sqlite-")
os.environ["KHOJ_AUTH_SECRET"] = "test-secret-for-local-tests"
os.environ["KHOJ_SUPER_ADMIN_EMAIL"] = "boss@example.com"
os.environ.setdefault("OPENRAG_URL", "http://openrag-backend:8000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main  # noqa: E402
from app.db import Users, connect  # noqa: E402
from app.orgs import SqliteOrgService  # noqa: E402
from app.storage import LocalStorage  # noqa: E402

# --- passwords + tokens ------------------------------------------------------
h, s = auth.hash_password("correct horse")
assert auth.check_password("correct horse", h, s)
assert not auth.check_password("wrong", h, s)

tok = auth.issue_token("u-1", "a@x.com")
assert auth.verify_token(tok) == {"uid": "u-1", "email": "a@x.com"}
try:
    auth.verify_token(tok[:-2] + "xx")
    raise SystemExit("tampered token verified!")
except ValueError:
    pass

# --- users -------------------------------------------------------------------
users = Users()
u = users.create_user("Person@X.com", "password-123")
assert users.get_by_email("person@x.com")["uid"] == u["uid"]
assert users.authenticate("person@x.com", "password-123")["uid"] == u["uid"]
assert users.authenticate("person@x.com", "nope") is None
assert users.authenticate("ghost@x.com", "password-123") is None

# --- SQLite org service -------------------------------------------------------
svc = SqliteOrgService(connect)
org = svc.create_org("Acme", {"uid": "u-9", "email": "o@x.com"})
assert svc.membership("u-9", org["id"]) == "admin"
assert svc.get_org_by_slug(org["slug"])["id"] == org["id"]
svc.set_org_status(org["id"], "approved")
assert svc.list_orgs_by_status("approved")[0]["id"] == org["id"]
inv = svc.create_invite(org["id"], "new@x.com", "member", "u-9", "hash")
assert svc.invites_for_email("new@x.com")[0]["orgId"] == org["id"]
svc.record_upload({"docId": "doc-1", "scopeKey": f"org:{org['id']}",
                   "filename": "a.txt", "uid": "u-9", "orgId": org["id"]})
assert svc.list_uploads(f"org:{org['id']}")[0]["docId"] == "doc-1"
assert svc.list_all_uploads()[0]["docId"] == "doc-1"
svc.log_event({"kind": "search", "query": "q"})
assert svc.list_events(10)[0]["kind"] == "search"

# --- storage: local backend ----------------------------------------------------
store = LocalStorage(os.path.join(os.environ["KHOJ_DATA_DIR"], "files"))
store.put("users/u-1/notes.md", b"# hi", "text/markdown")
assert store.get("users/u-1/notes.md") == b"# hi"
assert store.get("users/u-1/missing.md") is None
assert store.backend == "local"

# --- full API flow with REAL auth ----------------------------------------------
client = TestClient(main.app, raise_server_exceptions=False)


class Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)[:300]

    def json(self):
        return self._payload


posts = []


def fake_post(url, json=None, files=None, headers=None, timeout=None):
    posts.append(url)
    if url.endswith("/router/upload_ingest"):
        return Resp({"task_id": "task-1"}, status=202)
    if url.endswith("/knowledge-filter"):
        return Resp({}, status=201)
    if url.endswith("/search"):
        return Resp({"results": []})
    raise AssertionError(f"unexpected POST {url}")


main.requests.post = fake_post

r = client.post("/api/auth/register",
                json={"email": "boss@example.com", "password": "password-123"})
assert r.status_code == 200, r.text
boss = {"Authorization": "Bearer " + r.json()["token"]}

# the configured super-admin email bootstraps platform admin at once
r = client.get("/api/me", headers=boss)
assert r.json()["superAdmin"] is True, r.json()

r = client.post("/api/auth/register",
                json={"email": "boss@example.com", "password": "password-123"})
assert r.status_code == 409, r.status_code
r = client.post("/api/auth/login",
                json={"email": "boss@example.com", "password": "wrong-pass"})
assert r.status_code == 401, r.status_code
r = client.get("/api/me", headers={"Authorization": "Bearer garbage"})
assert r.status_code == 401, r.status_code
r = client.get("/api/me")
assert r.status_code == 401, r.status_code

# upload with local-disk storage, then read the original back
r = client.post(
    "/api/upload",
    files={"file": ("notes.md", io.BytesIO(b"# hello khoj"), "text/markdown")},
    headers=boss,
)
assert r.status_code == 200, r.text
doc_id = r.json()["documentId"]
data_dir = os.environ["KHOJ_DATA_DIR"]
on_disk = os.path.join(data_dir, "files", "users",
                       main._users.get_by_email("boss@example.com")["uid"],
                       "notes.md")
assert open(on_disk, "rb").read() == b"# hello khoj"

r = client.get(f"/api/documents/{doc_id}/raw", headers=boss)
assert r.status_code == 200 and r.content == b"# hello khoj", r.status_code

# a second user cannot read the first user's original
r = client.post("/api/auth/register",
                json={"email": "other@example.com", "password": "password-456"})
other = {"Authorization": "Bearer " + r.json()["token"]}
r = client.get(f"/api/documents/{doc_id}/raw", headers=other)
assert r.status_code == 403, r.status_code

# agent corpus endpoint: authorized list for the caller only
r = client.get("/api/agent/corpus", headers=boss)
names = [f["openragName"] for f in r.json()["files"]]
assert names == [f"{doc_id}--notes.md"], names
r = client.get("/api/agent/corpus", headers=other)
assert r.json()["files"] == [], r.json()

print("ALL SQLITE-STACK TESTS PASSED")
