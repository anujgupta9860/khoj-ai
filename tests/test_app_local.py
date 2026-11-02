"""Local wiring tests: MCP endpoint, per-user isolation, upload record.

Runs fully offline - Firebase, GCP auth, GCS and the Discovery Engine
HTTP calls are stubbed. Run:  python tests/test_app_local.py
"""

import io
import json
import os
import sys

os.environ.setdefault("GCP_PROJECT", "innovation-lab-2026")
os.environ.setdefault("KHOJ_ENGINE_ID", "khoj-search")
os.environ.setdefault("KHOJ_DATASTORE_ID", "khoj-corpus_test")
os.environ.setdefault("KHOJ_BUCKET", "khoj-ai-corpus-innovation-lab")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import firebase_admin

firebase_admin.initialize_app = lambda *a, **k: None  # no ADC locally

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

USER = {"uid": "user-123", "email": "u@example.com"}
REAL_REQUIRE_USER = main.require_user
REAL_RUN_SEARCH = main.run_search
REAL_LIST_DOCUMENTS = main.list_documents
main.require_user = lambda authorization: USER
main.gcp_access_token = lambda: "fake-token"

captured = {}


def fake_run_search(query, user, page_size=10, org_id=None):
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

assert main.user_filter({"uid": "abc"}) == 'user_id: ANY("abc")'

# --- upload: structData record carries the caller's uid --------------------
blobs = {}


class FakeBlob:
    def __init__(self, name):
        self.name = name

    def upload_from_file(self, fh, content_type=None):
        blobs[self.name] = ("file", content_type)

    def upload_from_string(self, data, content_type=None):
        blobs[self.name] = ("str", data)


class FakeBucket:
    def blob(self, name):
        return FakeBlob(name)


class FakeClient:
    def __init__(self, project=None):
        pass

    def bucket(self, name):
        return FakeBucket()


main.storage.Client = FakeClient


class Resp:
    status_code = 200

    def json(self):
        return {"name": "operations/import-1"}


main.requests.post = lambda *a, **k: Resp()

r = client.post(
    "/api/upload",
    files={"file": ("notes.md", io.BytesIO(b"# hi"), "text/markdown")},
    headers=H,
)
assert r.status_code == 200, r.text
meta = [v for k, v in blobs.items() if k.startswith("meta/users/user-123/")]
assert meta and meta[0][0] == "str"
record = json.loads(meta[0][1])
assert record["structData"]["user_id"] == "user-123", record
assert record["content"]["uri"].endswith("users/user-123/notes.md"), record
assert r.json()["documentId"] == record["id"]

# --- Businesses: shared corpus with membership enforced ----------------------
from types import SimpleNamespace

from app.orgs import InMemoryOrgService

main._org_service = InMemoryOrgService()
main.run_search = REAL_RUN_SEARCH
main.list_documents = REAL_LIST_DOCUMENTS

CURRENT = {"user": USER}
main.require_user = lambda authorization: CURRENT["user"]

search_payloads = []


class SearchResp:
    status_code = 200

    def json(self):
        return {
            "summary": {"summaryText": "S"},
            "totalSize": 0,
            "results": [],
            "name": "operations/x",
        }


def vertex_post(url, json=None, headers=None, timeout=None):
    search_payloads.append(json)
    return SearchResp()


main.requests.post = vertex_post


class DocsResp:
    status_code = 200

    def json(self):
        return {
            "documents": [
                {"id": "d1", "structData": {"user_id": "user-123", "title": "mine.md"}},
                {"id": "d2", "structData": {"org_id": ORG_ID, "title": "biz.md"}},
            ]
        }


ORG_ID = None  # set below before DocsResp is used
main.requests.get = lambda *a, **k: DocsResp()

# admin (enroller) creates a business
r = client.post("/api/orgs", json={"name": "Acme Inc"}, headers=H)
assert r.status_code == 200, r.text
org = r.json()
assert org["role"] == "admin" and org["name"] == "Acme Inc"
ORG_ID = org["id"]

r = client.get("/api/orgs", headers=H)
assert [o["id"] for o in r.json()["orgs"]] == [ORG_ID]

# personal search uses the user filter; org search uses the org filter
client.post("/api/search", json={"query": "q"}, headers=H)
assert search_payloads[-1]["filter"] == 'user_id: ANY("user-123")'
client.post("/api/search", json={"query": "q", "orgId": ORG_ID}, headers=H)
assert search_payloads[-1]["filter"] == f'org_id: ANY("{ORG_ID}")'

# document listing is scoped the same way
r = client.get("/api/documents", headers=H)
assert [d["title"] for d in r.json()["documents"]] == ["mine.md"]
r = client.get(f"/api/documents?orgId={ORG_ID}", headers=H)
assert [d["title"] for d in r.json()["documents"]] == ["biz.md"]

# add a member (email resolved through Firebase Auth)
main.fb_auth.get_user_by_email = lambda email: SimpleNamespace(uid="member-9", email=email)
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

# the admin can - and the record is scoped org_id, NOT user_id
CURRENT["user"] = USER
blobs.clear()
r = client.post(
    "/api/upload",
    files={"file": ("team.md", io.BytesIO(b"# t"), "text/markdown")},
    data={"orgId": ORG_ID},
    headers=H,
)
assert r.status_code == 200, r.text
meta = [v for k, v in blobs.items() if k.startswith(f"meta/orgs/{ORG_ID}/")]
assert meta, list(blobs)
record = json.loads(meta[0][1])
assert record["structData"]["org_id"] == ORG_ID and "user_id" not in record["structData"]
assert record["content"]["uri"].endswith(f"orgs/{ORG_ID}/team.md")

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
assert search_payloads[-1]["filter"] == f'org_id: ANY("{ORG_ID}")'
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

print("ALL LOCAL TESTS PASSED")
