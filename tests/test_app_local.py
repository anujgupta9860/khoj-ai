"""Local wiring tests: MCP endpoint, per-user isolation, upload record.

Runs fully offline - auth is bypassed at require_user, the storage
backend is a fake, and the OpenRAG HTTP calls are stubbed. Metadata
runs on a throwaway SQLite dir. Run:  python tests/test_app_local.py
"""

import io
import os
import sys
import tempfile

os.environ.setdefault("OPENRAG_URL", "http://openrag-backend:8000")
os.environ["KHOJ_DATA_DIR"] = tempfile.mkdtemp(prefix="khoj-test-")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

USER = {"uid": "user-123", "email": "u@example.com"}
REAL_REQUIRE_USER = main.require_user
REAL_RUN_SEARCH = main.run_search
REAL_LIST_DOCUMENTS = main.list_documents
main.require_user = lambda authorization: USER

captured = {}


def fake_run_search(query, user, page_size=10, org_id=None, source="web"):
    captured["search"] = (query, user["uid"], page_size)
    return {
        "summary": "S.",
        "totalSize": 1,
        "results": [{"title": "doc.md", "link": "", "snippets": ["x"], "extractive": []}],
    }


main.run_search = fake_run_search
main.list_documents = lambda user, org_id=None: [{"id": "doc-1", "title": "doc.md"}]

client = TestClient(main.app, raise_server_exceptions=False)
H = {"Authorization": "Bearer whatever"}


def rpc(payload, headers=H):
    return client.post("/mcp", json=payload, headers=headers)


class Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)[:300]

    def json(self):
        return self._payload


# --- MCP handshake + tools -------------------------------------------------
r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
assert r.status_code == 200, r.text
assert r.json()["result"]["serverInfo"]["name"] == "khoj-ai"
assert r.json()["result"]["protocolVersion"] == "2025-03-26"

r = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
assert r.status_code == 202, r.status_code

r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
names = [t["name"] for t in r.json()["result"]["tools"]]
assert names == ["search_documents", "list_my_documents", "list_my_corpora"], names

r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_documents", "arguments": {"query": "warranty"}}})
res = r.json()["result"]
assert res["isError"] is False and "doc.md" in res["content"][0]["text"]
assert res["structuredContent"]["totalSize"] == 1
assert captured["search"] == ("warranty", "user-123", 10), captured

r = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "list_my_documents", "arguments": {}}})
assert "doc.md" in r.json()["result"]["content"][0]["text"]

r = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}})
assert r.json()["error"]["code"] == -32602

r = rpc({"jsonrpc": "2.0", "id": 6, "method": "bogus/method"})
assert r.status_code == 404 and r.json()["error"]["code"] == -32601

assert client.get("/mcp").status_code == 405

# --- auth gate: the real require_user rejects missing/bogus tokens ---------
from fastapi import HTTPException

for bad in (None, "Bearer not-a-token"):
    try:
        REAL_REQUIRE_USER(bad)
        raise AssertionError(f"require_user accepted {bad!r}")
    except HTTPException as e:
        assert e.status_code == 401, e.status_code

# --- corpus naming: doc ids are stable, OpenRAG names carry the scope -----
assert main.document_id("users/abc", "x.md").startswith("doc-")
assert main.document_id("users/abc", "x.md") != main.document_id("orgs/o1", "x.md")
assert main.openrag_filename("doc-1", "x.md") == "doc-1--x.md"
assert main.scope_of({"uid": "abc"}, None) == ("users/abc", "user:abc")
assert main.scope_of({"uid": "abc"}, "org-9") == ("orgs/org-9", "org:org-9")

# --- upload: file kept in the storage backend, ingested into OpenRAG ----
blobs = {}


class FakeStorage:
    backend = "fake"

    def put(self, key, data, mime=""):
        blobs[key] = ("str", data)

    def get(self, key):
        item = blobs.get(key)
        return item[1] if item else None


main.get_storage = lambda: FakeStorage()

ingest_calls = []


def first_post(url, json=None, files=None, headers=None, timeout=None):
    ingest_calls.append((url, files["file"][0] if files else None))
    return Resp({"task_id": "task-0"}, status=202)


main.requests.post = first_post

r = client.post(
    "/api/upload",
    files={"file": ("notes.md", io.BytesIO(b"# hi"), "text/markdown")},
    headers=H,
)
assert r.status_code == 200, r.text
doc_id = main.document_id("users/user-123", "notes.md")
assert r.json()["documentId"] == doc_id
assert r.json()["task"] == "task-0"
assert "users/user-123/notes.md" in blobs
assert not any(k.startswith("meta/") for k in blobs), list(blobs)
assert [c for c in ingest_calls if c[1]][-1][1] == main.openrag_filename(doc_id, "notes.md")

# --- Businesses: shared corpus with membership enforced ----------------------
from types import SimpleNamespace

from app.orgs import InMemoryOrgService

main._org_service = InMemoryOrgService()
main.run_search = REAL_RUN_SEARCH
main.list_documents = REAL_LIST_DOCUMENTS

CURRENT = {"user": USER}
main.require_user = lambda authorization: CURRENT["user"]

# corpus fixtures: mine.md (personal) and biz.md (business), present
# both as upload records and as indexed OpenRAG documents
MINE_ID = main.document_id("users/user-123", "mine.md")
MINE_NAME = main.openrag_filename(MINE_ID, "mine.md")
main._org_service.record_upload({
    "docId": MINE_ID, "scopeKey": "user:user-123", "uid": "user-123",
    "orgId": None, "filename": "mine.md", "openragTask": "task-0",
    "uploadedAt": 0,
})
GLOBAL_ID = main.document_id("orgs/khoj", "global.md")
GLOBAL_NAME = main.openrag_filename(GLOBAL_ID, "global.md")
main._org_service.record_upload({
    "docId": GLOBAL_ID, "scopeKey": "org:khoj", "uid": "admin-0",
    "orgId": "khoj", "filename": "global.md", "openragTask": "task-0",
    "uploadedAt": 0,
})
CORPUS = {"biz_name": None, "team_name": None}

search_payloads = []
upload_names = []
filter_calls = []


def openrag_post(url, json=None, files=None, headers=None, timeout=None):
    from urllib.parse import urlsplit
    path = urlsplit(url).path  # backend routes are unprefixed; /api/* is frontend-proxy only
    if path == "/router/upload_ingest":
        upload_names.append(files["file"][0])
        return Resp({"task_id": "task-1"}, status=202)
    if path == "/knowledge-filter":
        filter_calls.append(json)
        return Resp({"success": True, "id": "kf-1"})
    if path == "/search":
        search_payloads.append(json)
        if (json or {}).get("query") == "zzz-floor":
            # baseline kNN noise: real text but a junk-level score
            return Resp({"results": [
                {"filename": MINE_NAME, "text": "some chunk text", "score": 1.27},
            ]})
        if (json or {}).get("query") == "zzz-no-match":
            # OpenRAG shell hits: filename present, text empty
            return Resp({"results": [
                {"filename": MINE_NAME, "text": "", "score": 0.1},
                {"filename": GLOBAL_NAME, "text": "", "score": 0.05},
            ]})
        results = [
            {"filename": MINE_NAME, "text": "mine <b>text</b>&nbsp;here", "score": 2.0},
            {"filename": GLOBAL_NAME, "text": "global handbook", "score": 1.8},
        ]
        if CORPUS["biz_name"]:
            results.append({"filename": CORPUS["biz_name"], "text": "biz", "score": 3.0})
        return Resp({"results": results})
    raise AssertionError(f"unexpected POST {url}")


def openrag_get(url, params=None, headers=None, timeout=None):
    from urllib.parse import urlsplit
    path = urlsplit(url).path
    if path == "/files":
        files = [{"filename": MINE_NAME, "chunk_count": 3},
                 {"filename": GLOBAL_NAME, "chunk_count": 5}]
        if CORPUS["biz_name"]:
            files.append({"filename": CORPUS["biz_name"], "chunk_count": 2})
        return Resp({"files": files})
    if "/tasks/" in url:
        return Resp({"status": "RUNNING"})
    raise AssertionError(f"unexpected GET {url}")


main.requests.post = openrag_post
main.requests.get = openrag_get

# admin (enroller) creates a business - it starts PENDING approval
r = client.post("/api/orgs", json={"name": "Acme Inc"}, headers=H)
assert r.status_code == 200, r.text
org = r.json()
assert org["role"] == "admin" and org["name"] == "Acme Inc"
assert org["status"] == "pending", org
ORG_ID = org["id"]
# simulate the super-admin approval (the gated flow itself is tested below)
main._org_service.set_org_status(ORG_ID, "approved")

BIZ_ID = main.document_id(f"orgs/{ORG_ID}", "biz.md")
CORPUS["biz_name"] = main.openrag_filename(BIZ_ID, "biz.md")
main._org_service.record_upload({
    "docId": BIZ_ID, "scopeKey": f"org:{ORG_ID}", "uid": "user-123",
    "orgId": ORG_ID, "filename": "biz.md", "openragTask": "task-0",
    "uploadedAt": 0,
})

r = client.get("/api/orgs", headers=H)
assert [o["id"] for o in r.json()["orgs"]] == [ORG_ID]

# --- file list: every upload appears at once, with an ingestion status -----
r = client.post(
    "/api/upload",
    files={"file": ("status.md", io.BytesIO(b"# s"), "text/markdown")},
    headers=H,
)
assert r.status_code == 200, r.text
new_doc = r.json()["documentId"]
STATUS_NAME = main.openrag_filename(new_doc, "status.md")
assert upload_names[-1] == STATUS_NAME
# the upload also registered a knowledge filter (OpenRAG Knowledge tab)
assert filter_calls and filter_calls[-1]["name"] == "status", filter_calls
assert filter_calls[-1]["queryData"]["filters"]["data_sources"] == [STATUS_NAME]
r = client.get("/api/documents", headers=H)
docs = {d["id"]: d for d in r.json()["documents"]}
assert docs[MINE_ID]["status"] == "ready", docs        # already in OpenRAG
assert docs[GLOBAL_ID]["status"] == "ready" and docs[GLOBAL_ID]["corpus"] == "global", docs
assert docs[MINE_ID]["corpus"] == "own", docs
assert docs[new_doc]["status"] == "indexing", docs     # uploaded, not indexed yet
assert docs[new_doc]["title"] == "status.md", docs

# search scope is enforced by an OpenRAG filename filter naming exactly
# this corpus's documents, plus post-filtering: other corpora's hits
# are dropped even when the engine returns them
r = client.post("/api/search", json={"query": "q"}, headers=H)
assert [x["title"] for x in r.json()["results"]] == ["mine.md", "global.md"]

assert r.json()["results"][0]["snippets"] == ["mine text here"]  # markup/entities stripped
assert sorted(search_payloads[-1]["filters"]["filename"]) == sorted(
    [MINE_NAME, STATUS_NAME, GLOBAL_NAME])

# shell hits (filename with empty excerpt) are not results
r = client.post("/api/search", json={"query": "zzz-no-match"}, headers=H)
assert r.json()["totalSize"] == 0 and r.json()["results"] == [], r.json()

# baseline-score chunks (unrelated-query noise) are not results either
r = client.post("/api/search", json={"query": "zzz-floor"}, headers=H)
assert r.json()["totalSize"] == 0 and r.json()["results"] == [], r.json()
r = client.post("/api/search", json={"query": "q", "orgId": ORG_ID}, headers=H)
assert [x["title"] for x in r.json()["results"]] == ["biz.md", "global.md"]
assert sorted(search_payloads[-1]["filters"]["filename"]) == sorted(
    [CORPUS["biz_name"], GLOBAL_NAME])

# document listing is scoped the same way (status.md is the same user's
# upload from the status test above; biz.md must NOT leak into this list)
r = client.get("/api/documents", headers=H)
assert [d["title"] for d in r.json()["documents"]] == ["global.md", "mine.md", "status.md"]
r = client.get(f"/api/documents?orgId={ORG_ID}", headers=H)
assert [d["title"] for d in r.json()["documents"]] == ["biz.md", "global.md"]

# add a member (email resolved against the account store)
main._users.get_by_email = lambda email: {"uid": "member-9", "email": email}
r = client.post(f"/api/orgs/{ORG_ID}/members", json={"email": "m@x.com"}, headers=H)
assert r.status_code == 200 and r.json()["role"] == "member", r.text

# the member can search the business corpus; a stranger cannot
CURRENT["user"] = {"uid": "member-9", "email": "m@x.com"}
r = client.post("/api/search", json={"query": "q", "orgId": ORG_ID}, headers=H)
assert r.status_code == 200, r.text
CURRENT["user"] = {"uid": "stranger-1", "email": "s@x.com"}
r = client.post("/api/search", json={"query": "q", "orgId": ORG_ID}, headers=H)
assert r.status_code == 403, r.text
r = client.get("/api/orgs", headers=H)
assert r.json()["orgs"] == []

# a plain member cannot upload into the business corpus
CURRENT["user"] = {"uid": "member-9", "email": "m@x.com"}
r = client.post(
    "/api/upload",
    files={"file": ("team.md", io.BytesIO(b"# t"), "text/markdown")},
    data={"orgId": ORG_ID},
    headers=H,
)
assert r.status_code == 403, r.text

# the admin can - the file lands in the org's storage prefix and goes to
# OpenRAG under the org-scoped doc name
CURRENT["user"] = USER
blobs.clear()
r = client.post(
    "/api/upload",
    files={"file": ("team.md", io.BytesIO(b"# t"), "text/markdown")},
    data={"orgId": ORG_ID},
    headers=H,
)
assert r.status_code == 200, r.text
assert f"orgs/{ORG_ID}/team.md" in blobs
TEAM_ID = main.document_id(f"orgs/{ORG_ID}", "team.md")
CORPUS["team_name"] = main.openrag_filename(TEAM_ID, "team.md")
assert upload_names[-1] == CORPUS["team_name"]
assert r.json()["documentId"] == TEAM_ID

# MCP: corpora discovery + org-scoped search through the agent surface
CURRENT["user"] = {"uid": "member-9", "email": "m@x.com"}
r = rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "list_my_corpora", "arguments": {}}})
corpora = r.json()["result"]["structuredContent"]["corpora"]
assert any(c.get("id") == ORG_ID and c["type"] == "business" for c in corpora), corpora
r = rpc({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "search_documents",
                    "arguments": {"query": "q", "orgId": ORG_ID}}})
assert r.json()["result"]["isError"] is False, r.text
mcp_text = r.json()["result"]["content"][0]["text"]
assert "biz.md" in mcp_text and "global.md" in mcp_text  # org + global
assert "mine.md" not in mcp_text  # another corpus never leaks in
assert sorted(search_payloads[-1]["filters"]["filename"]) == sorted(
    [CORPUS["biz_name"], CORPUS["team_name"], GLOBAL_NAME])
CURRENT["user"] = USER

# --- Invitations + branding ---------------------------------------------------
CURRENT["user"] = USER  # the org admin

# invite by email; the link carries a one-time secret
r = client.post(f"/api/orgs/{ORG_ID}/invites", json={"email": "Emp@X.com"}, headers=H)
assert r.status_code == 200, r.text
inv = r.json()
assert inv["email"] == "emp@x.com" and inv["inviteLink"].startswith("http")
token = inv["inviteLink"].split("invite=")[1]
t_org, t_inv, t_secret = token.split(".")

r = client.get(f"/api/orgs/{ORG_ID}/invites", headers=H)
assert [i["email"] for i in r.json()["invites"]] == ["emp@x.com"]

# the invitee signs up directly and sees + accepts the invite (email match)
CURRENT["user"] = {"uid": "emp-1", "email": "emp@x.com"}
r = client.get("/api/invites", headers=H)
assert len(r.json()["invites"]) == 1 and r.json()["invites"][0]["orgId"] == ORG_ID
r = client.post("/api/invites/accept", json={"orgId": ORG_ID, "inviteId": inv["inviteId"]}, headers=H)
assert r.status_code == 200 and r.json()["role"] == "member", r.text
r = client.post("/api/search", json={"query": "q", "orgId": ORG_ID}, headers=H)
assert r.status_code == 200  # now a member

# a second accept fails - no longer pending
r = client.post("/api/invites/accept", json={"orgId": ORG_ID, "inviteId": inv["inviteId"]}, headers=H)
assert r.status_code == 404

# link-secret path: invite for a different email, accepted via the secret
CURRENT["user"] = USER
r = client.post(f"/api/orgs/{ORG_ID}/invites", json={"email": "other@x.com", "role": "admin"}, headers=H)
inv2 = r.json()
t2 = inv2["inviteLink"].split("invite=")[1].split(".")
CURRENT["user"] = {"uid": "emp-2", "email": "different@x.com"}
r = client.post(
    "/api/invites/accept",
    json={"orgId": t2[0], "inviteId": t2[1], "secret": "wrong"},
    headers=H,
)
assert r.status_code == 403, r.text
r = client.post(
    "/api/invites/accept",
    json={"orgId": t2[0], "inviteId": t2[1], "secret": t2[2]},
    headers=H,
)
assert r.status_code == 200 and r.json()["role"] == "admin", r.text

# revoked invites cannot be accepted
CURRENT["user"] = USER
r = client.post(f"/api/orgs/{ORG_ID}/invites", json={"email": "gone@x.com"}, headers=H)
inv3 = r.json()
r = client.delete(f"/api/orgs/{ORG_ID}/invites/{inv3['inviteId']}", headers=H)
assert r.status_code == 200
CURRENT["user"] = {"uid": "emp-3", "email": "gone@x.com"}
r = client.post("/api/invites/accept", json={"orgId": ORG_ID, "inviteId": inv3["inviteId"]}, headers=H)
assert r.status_code == 404

# the last remaining admin cannot be removed (emp-2 is also admin now, so
# removing USER is fine; removing emp-2 afterwards must fail as last admin)
CURRENT["user"] = USER
r = client.delete(f"/api/orgs/{ORG_ID}/members/emp-2", headers=H)
assert r.status_code == 200
r = client.delete(f"/api/orgs/{ORG_ID}/members/user-123", headers=H)
assert r.status_code == 400, r.text

# branding: admin sets it, bad color rejected, member cannot change it
r = client.put(
    f"/api/orgs/{ORG_ID}/branding",
    json={"displayName": "Acme Docs", "primaryColor": "#0ea5e9", "tagline": "Acme knowledge base"},
    headers=H,
)
assert r.status_code == 200 and r.json()["branding"]["displayName"] == "Acme Docs", r.text
r = client.put(f"/api/orgs/{ORG_ID}/branding", json={"primaryColor": "blue"}, headers=H)
assert r.status_code == 422
r = client.put(f"/api/orgs/{ORG_ID}/branding", json={"logoUrl": "http://insecure.example/x.png"}, headers=H)
assert r.status_code == 422
slug = client.get("/api/orgs", headers=H).json()["orgs"][0]["slug"]
assert slug.startswith("acme-inc-"), slug
r = client.get(f"/api/public/org/{slug}")  # no auth needed
assert r.status_code == 200 and r.json()["branding"]["displayName"] == "Acme Docs", r.text
CURRENT["user"] = {"uid": "member-9", "email": "m@x.com"}
r = client.put(f"/api/orgs/{ORG_ID}/branding", json={"displayName": "Hijack"}, headers=H)
assert r.status_code == 403
CURRENT["user"] = USER

# --- Super admin: org approval gate, conversations, metrics -------------------
svc = main._org_service

# a fresh org is pending: its corpus is closed, even to its own admin
CURRENT["user"] = USER
r = client.post("/api/orgs", json={"name": "Beta LLC"}, headers=H)
org2 = r.json()
assert org2["status"] == "pending"
r = client.post("/api/search", json={"query": "q", "orgId": org2["id"]}, headers=H)
assert r.status_code == 403, r.text
# ...but its admin may still set branding while waiting
r = client.put(f"/api/orgs/{org2['id']}/branding", json={"displayName": "Beta"}, headers=H)
assert r.status_code == 200

# non-super users cannot touch the admin surface
CURRENT["user"] = {"uid": "member-9", "email": "m@x.com"}
for path in ("/api/admin/org-requests", "/api/admin/logs", "/api/admin/metrics",
             "/api/admin/conversations", "/api/admin/uploads", "/api/admin/file-stats"):
    assert client.get(path, headers=H).status_code == 403, path
r = client.post(f"/api/admin/orgs/{org2['id']}/decision", json={"decision": "approve"}, headers=H)
assert r.status_code == 403

# USER becomes super admin via the platform org (Khoj.ai itself)
svc.ensure_platform_org(USER)
CURRENT["user"] = USER
r = client.get("/api/me", headers=H)
assert r.json()["superAdmin"] is True
r = client.get("/api/admin/org-requests", headers=H)
assert [o["id"] for o in r.json()["orgs"]] == [org2["id"]], r.json()

# approve -> the corpus opens
r = client.post(f"/api/admin/orgs/{org2['id']}/decision", json={"decision": "approve"}, headers=H)
assert r.status_code == 200 and r.json()["status"] == "approved"
r = client.post("/api/search", json={"query": "beta pricing", "orgId": org2["id"]}, headers=H)
assert r.status_code == 200, r.text

# reject path: org3 belongs to emp-1; after rejection ITS OWN admin is
# blocked (the super admin keeps an inspection bypass by design)
CURRENT["user"] = {"uid": "emp-1", "email": "emp@x.com"}
r = client.post("/api/orgs", json={"name": "Gamma LLC"}, headers=H)
org3 = r.json()
CURRENT["user"] = USER
r = client.post(f"/api/admin/orgs/{org3['id']}/decision", json={"decision": "reject"}, headers=H)
assert r.json()["status"] == "rejected"
CURRENT["user"] = {"uid": "emp-1", "email": "emp@x.com"}
r = client.post("/api/search", json={"query": "q", "orgId": org3["id"]}, headers=H)
assert r.status_code == 403
CURRENT["user"] = USER

# conversations + metrics: the searches above were logged
r = client.get("/api/admin/logs?limit=50", headers=H)
logs = r.json()["logs"]
assert any(e.get("query") == "beta pricing" and e.get("orgId") == org2["id"] for e in logs), logs[:3]
assert any(e.get("kind") == "upload" for e in logs)
r = client.get("/api/admin/metrics", headers=H)
m = r.json()
assert m["orgs"]["approved"] >= 2 and m["orgs"]["rejected"] == 1, m["orgs"]
assert m["searches"]["total"] >= 3 and m["searches"]["uniqueUsers"] >= 1
assert m["searches"]["zeroResultRate"] < 1.0  # in-scope searches above returned results
CURRENT["user"] = USER

# super-admin conversations: questions WITH their answers, org names resolved
r = client.get("/api/admin/conversations?limit=50", headers=H)
convs = r.json()["conversations"]
hit = [c for c in convs if c["question"] == "beta pricing"]
assert hit and hit[0]["orgName"] == org2["name"], convs[:3]
assert all("answer" in c for c in convs)

# super-admin uploads: grouped global / per-org / personal
r = client.get("/api/admin/uploads", headers=H)
up = r.json()
assert any(f["filename"] == "global.md" for f in up["global"]), up["global"]
assert any(g["orgId"] == ORG_ID and any(f["filename"] == "biz.md" for f in g["files"])
           for g in up["orgs"]), up["orgs"]
assert any(f["filename"] == "mine.md" for g in up["personal"] for f in g["files"]), up["personal"]
assert all(set(f) >= {"filename", "uploadedAt", "status"} for g in up["orgs"] for f in g["files"])

# super-admin file matrix: chunks + ingestion time per file
r = client.get("/api/admin/file-stats", headers=H)
fs = r.json()
gf = [f for f in fs["global"] if f["filename"] == "global.md"]
assert gf and "chunks" in gf[0] and "ingestionSec" in gf[0] and gf[0]["status"] == "ready", fs["global"]
assert any(g["orgId"] == ORG_ID and any(f["filename"] == "biz.md" for f in g["files"])
           for g in fs["orgs"]), fs["orgs"]

# --- chat: the web service as an A2A client of the agent --------------------
os.environ["KHOJ_AGENT_URL"] = "http://khoj-agent"
agent_calls = []


class AgentResp:
    status_code = 200

    def json(self):
        return {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "kind": "message", "role": "agent", "contextId": "ctx-42",
                "parts": [{"kind": "text", "text": "Grounded answer."}],
                "metadata": {"sources": ["doc.md"]},
            },
        }


def agent_post(url, json=None, headers=None, timeout=None):
    agent_calls.append((url, json, headers))
    return AgentResp()


_real_post = main.requests.post
main.requests.post = agent_post
r = client.post("/api/chat", json={"message": "what is in doc.md?"}, headers=H)
assert r.status_code == 200, r.text
assert r.json()["reply"] == "Grounded answer." and r.json()["contextId"] == "ctx-42"
rpc_sent = agent_calls[0][1]
assert rpc_sent["method"] == "message/send"
assert rpc_sent["params"]["message"]["parts"][0]["text"] == "what is in doc.md?"
assert agent_calls[0][2]["Authorization"] == "Bearer whatever"  # caller token forwarded
# recent chats: the sidebar's last-5 list, grouped by contextId
r = client.post("/api/chat", json={"message": "and what else?", "contextId": "ctx-42"}, headers=H)
assert r.status_code == 200, r.text
r = client.get("/api/chats", headers=H)
assert r.status_code == 200, r.text
chats = r.json()["chats"]
assert len(chats) == 1 and chats[0]["contextId"] == "ctx-42", chats
assert chats[0]["title"] == "what is in doc.md?", chats[0]
assert [m["role"] for m in chats[0]["messages"]] == ["user", "assistant", "user", "assistant"]
assert chats[0]["messages"][0]["text"] == "what is in doc.md?"
assert chats[0]["messages"][1]["text"] == "Grounded answer."
# isolation: another user's chat events never leak into my list
main.org_service().log_event({"kind": "chat", "uid": "someone-else", "email": "x@example.com",
                              "contextId": "ctx-other", "query": "not mine", "answer": "nope"})
chats = client.get("/api/chats", headers=H).json()["chats"]
assert all(c["contextId"] != "ctx-other" for c in chats), chats
# --- Jira ticket: configured components / affects-versions ride along --
os.environ.update({
    "JIRA_BASE_URL": "https://sandbox.example.atlassian.net",
    "JIRA_EMAIL": "u@example.com",
    "JIRA_API_TOKEN": "tok",
    "JIRA_PROJECT_KEY": "CCS",
    "JIRA_COMPONENTS": "Platform, Cloud",
    "JIRA_AFFECTS_VERSIONS": "R24.4",
})
jira_captured = {}
_prev_post_for_jira = main.requests.post


def jira_post(url, json=None, files=None, headers=None, timeout=None, **kw):
    if url.endswith("/rest/api/3/issue"):
        jira_captured["url"] = url
        jira_captured["json"] = json

        class R:
            status_code = 201
            text = ""

            def json(self):
                return {"key": "CCS-42"}

        return R()
    return _prev_post_for_jira(url, json=json, files=files, headers=headers, timeout=timeout, **kw)


main.requests.post = jira_post
r = client.post("/api/jira/ticket", json={"question": "What is EXOS?", "answer": "A router OS."}, headers=H)
assert r.status_code == 200, r.text
assert r.json()["key"] == "CCS-42", r.json()
_jf = jira_captured["json"]["fields"]
assert _jf["components"] == [{"name": "Platform"}, {"name": "Cloud"}], _jf
assert _jf["versions"] == [{"name": "R24.4"}], _jf

# --- /api/jira/ticket with a conversation transcript: the ticket gets the
# agent's LLM summary plus the full conversation; a summarize failure
# falls back to the plain question/answer ticket.
import json as _json2


def jira_post_with_summary(url, json=None, files=None, headers=None, timeout=None, **kw):
    if url.endswith("/summarize"):
        jira_captured["summarize_json"] = json

        class RS:
            status_code = 200
            text = ""

            def json(self):
                return {"summary": "**Issue**\nVPN drops hourly."}

        return RS()
    return jira_post(url, json=json, files=files, headers=headers, timeout=timeout, **kw)


main.requests.post = jira_post_with_summary
jira_captured.clear()
r = client.post("/api/jira/ticket", json={
    "question": "Why does my VPN drop?",
    "answer": "Check the tunnel keepalive.",
    "transcript": [
        {"role": "user", "text": "My VPN drops every hour"},
        {"role": "agent", "text": "Check the tunnel keepalive."},
        {"role": "user", "text": "Still drops"},
    ],
}, headers=H)
assert r.status_code == 200, r.text
assert jira_captured["summarize_json"]["transcript"][0]["text"].startswith("My VPN"), jira_captured
_desc = _json2.dumps(jira_captured["json"]["fields"]["description"])
assert "Conversation summary" in _desc and "VPN drops hourly." in _desc, _desc
assert "Full conversation" in _desc and "Customer: My VPN drops every hour" in _desc, _desc


def jira_post_no_summary(url, json=None, files=None, headers=None, timeout=None, **kw):
    if url.endswith("/summarize"):
        class RF:
            status_code = 502
            text = "down"

            def json(self):
                return {}

        return RF()
    return jira_post(url, json=json, files=files, headers=headers, timeout=timeout, **kw)


main.requests.post = jira_post_no_summary
jira_captured.clear()
r = client.post("/api/jira/ticket", json={
    "question": "Why does my VPN drop?",
    "answer": "Check the tunnel keepalive.",
    "transcript": [{"role": "user", "text": "My VPN drops every hour"}],
}, headers=H)
assert r.status_code == 200, r.text
_desc = _json2.dumps(jira_captured["json"]["fields"]["description"])
assert "Conversation summary" not in _desc and "Full conversation" not in _desc, _desc
main.requests.post = jira_post

# --- /api/jira/fields: prompt only for required fields config doesn't cover
CREATEMETA = {"projects": [{"issuetypes": [{"fields": {
    "summary": {"required": True, "name": "Summary", "allowedValues": []},
    "components": {"required": True, "name": "Components", "schema": {"type": "array"},
                   "allowedValues": [{"id": "1", "name": "Platform"}, {"id": "2", "name": "Cloud"}]},
    "versions": {"required": True, "name": "Affects versions", "schema": {"type": "array"},
                 "allowedValues": [{"id": "3", "name": "R24.4"}]},
    "labels": {"required": False, "name": "Labels", "allowedValues": []},
}}]}]}
_prev_get_for_jira = main.requests.get


def jira_get(url, params=None, headers=None, timeout=None, **kw):
    if "user/search" in url:
        class R:
            status_code = 200
            text = ""

            def json(self):
                return [{"accountId": "acc-9", "emailAddress": "krunal.patil@calix.com"}]

        return R()
    if "createmeta" in url:
        class R:
            status_code = 200
            text = ""

            def json(self):
                return CREATEMETA

        return R()
    return _prev_get_for_jira(url, params=params, headers=headers, timeout=timeout, **kw)


main.requests.get = jira_get
# config covers both fields -> nothing to prompt for
r = client.get("/api/jira/fields", headers=H)
assert r.status_code == 200 and r.json()["fields"] == [], r.text
# drop the config -> both required fields come back for prompting
_saved_c, _saved_v = os.environ.pop("JIRA_COMPONENTS"), os.environ.pop("JIRA_AFFECTS_VERSIONS")
r = client.get("/api/jira/fields", headers=H)
assert r.status_code == 200, r.text
_got = {f["key"]: f for f in r.json()["fields"]}
assert set(_got) == {"components", "versions"}, _got
assert _got["components"]["multi"] is True
assert [v["name"] for v in _got["versions"]["allowedValues"]] == ["R24.4"]
# prompted values land in the create payload
jira_captured.clear()
r = client.post("/api/jira/ticket",
                json={"question": "Q?", "answer": "A.",
                      "extraFields": {"components": ["Cloud"], "versions": ["R24.4"]}},
                headers=H)
assert r.status_code == 200, r.text
_jf = jira_captured["json"]["fields"]
assert _jf["components"] == [{"name": "Cloud"}], _jf
assert _jf["versions"] == [{"name": "R24.4"}], _jf
# a prompted Reporter resolves through user search to an accountId
jira_captured.clear()
r = client.post("/api/jira/ticket",
                json={"question": "Q?", "answer": "A.",
                      "extraFields": {"reporter": "krunal.patil@calix.com"}},
                headers=H)
assert r.status_code == 200, r.text
assert jira_captured["json"]["fields"]["reporter"] == {"accountId": "acc-9"}, jira_captured
# description keeps the answer's formatting (bold/list as real ADF)
# and drops the chat citation markers
import json as _json
jira_captured.clear()
r = client.post("/api/jira/ticket",
                json={"question": "What changed?",
                      "answer": "Wi-Fi 7 is **supported** [1].\n* **Mesh:** expanded coverage [1], [4]\n* Uses `MDI` agent"},
                headers=H)
assert r.status_code == 200, r.text
_desc = _json.dumps(jira_captured["json"]["fields"]["description"])
assert "bulletList" in _desc and '"strong"' in _desc and '"code"' in _desc, _desc
assert "[1]" not in _desc and "[4]" not in _desc, _desc
main.requests.get = _prev_get_for_jira
os.environ["JIRA_COMPONENTS"], os.environ["JIRA_AFFECTS_VERSIONS"] = _saved_c, _saved_v

main.requests.post = _prev_post_for_jira
for _k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
           "JIRA_COMPONENTS", "JIRA_AFFECTS_VERSIONS"):
    os.environ.pop(_k, None)

main.requests.post = _real_post

# --- document delete: chunks + original + filter + record all go --------
del_calls = []
kf_deleted = []


class FakeStorage2(FakeStorage):
    def delete(self, key):
        blobs.pop(key, None)


main.get_storage = lambda: FakeStorage2()
blobs["users/user-123/mine.md"] = ("str", b"# mine")


def delete_post(url, json=None, files=None, headers=None, timeout=None):
    from urllib.parse import urlsplit
    path = urlsplit(url).path
    if path == "/documents/delete-by-filename":
        del_calls.append(json["filename"])
        if json["filename"].endswith("fail.md"):
            return Resp({"error": "boom"}, status=500)
        if json["filename"].endswith("pending.md"):
            # still indexing: OpenRAG reports zero chunks as a failure
            return Resp({
                "success": False, "deleted_chunks": 0,
                "error": "No matching document chunks were deleted. "
                         "The file may be missing or not deletable.",
            }, status=404)
        return Resp({"success": True, "deleted_chunks": 3})
    if path == "/knowledge-filter/search":
        return Resp({"results": [{
            "id": "kf-9", "name": "mine",
            "description": "Uploaded via Khoj: mine.md",
            "queryData": {"filters": {"data_sources": [MINE_NAME]}},
        }]})
    raise AssertionError(f"unexpected POST {path}")


def fake_delete(url, timeout=None):
    kf_deleted.append(url)
    return Resp({"success": True})


_real_del = getattr(main.requests, "delete", None)
main.requests.post = delete_post
main.requests.delete = fake_delete

r = client.request("DELETE", f"/api/documents/{MINE_ID}", headers=H)
assert r.status_code == 200, r.text
assert r.json()["deleted"] == MINE_ID
assert del_calls == [MINE_NAME], del_calls
assert kf_deleted and kf_deleted[0].endswith("/knowledge-filter/kf-9"), kf_deleted
assert "users/user-123/mine.md" not in blobs
assert all(rec["docId"] != MINE_ID for rec in main._org_service.list_uploads("user:user-123"))
r = client.get(f"/api/documents/{MINE_ID}/raw", headers=H)
assert r.status_code == 404, r.status_code

# the global corpus is not deletable from a personal view
r = client.request("DELETE", f"/api/documents/{GLOBAL_ID}", headers=H)
assert r.status_code == 404, r.status_code
assert any(rec["docId"] == GLOBAL_ID for rec in main._org_service.list_uploads("org:khoj"))

# unknown id -> 404; OpenRAG failure -> 502 and the record survives
r = client.request("DELETE", "/api/documents/doc-nope", headers=H)
assert r.status_code == 404, r.status_code
FAIL_ID = main.document_id("users/user-123", "fail.md")
main._org_service.record_upload({
    "docId": FAIL_ID, "scopeKey": "user:user-123", "uid": "user-123",
    "orgId": None, "filename": "fail.md", "openragTask": "task-0",
    "uploadedAt": 0,
})
r = client.request("DELETE", f"/api/documents/{FAIL_ID}", headers=H)
assert r.status_code == 502, r.status_code
assert any(rec["docId"] == FAIL_ID for rec in main._org_service.list_uploads("user:user-123"))
main._org_service.remove_upload(FAIL_ID)

# a still-indexing document (no chunks yet) deletes cleanly too
PEND_ID = main.document_id("users/user-123", "pending.md")
main._org_service.record_upload({
    "docId": PEND_ID, "scopeKey": "user:user-123", "uid": "user-123",
    "orgId": None, "filename": "pending.md", "openragTask": "task-9",
    "uploadedAt": 0,
})
r = client.request("DELETE", f"/api/documents/{PEND_ID}", headers=H)
assert r.status_code == 200, r.text
assert all(rec["docId"] != PEND_ID for rec in main._org_service.list_uploads("user:user-123"))

main.requests.post = _real_post
if _real_del is not None:
    main.requests.delete = _real_del

print("ALL LOCAL TESTS PASSED")
