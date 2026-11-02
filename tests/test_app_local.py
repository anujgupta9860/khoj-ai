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
main.require_user = lambda authorization: USER
main.gcp_access_token = lambda: "fake-token"

captured = {}


def fake_run_search(query, user, page_size=10):
    captured["search"] = (query, user["uid"], page_size)
    return {
        "summary": "S.",
        "totalSize": 1,
        "results": [{"title": "doc.md", "link": "", "snippets": ["x"], "extractive": []}],
    }


main.run_search = fake_run_search
main.list_documents = lambda user: [{"id": "doc-1", "title": "doc.md"}]

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
assert names == ["search_documents", "list_my_documents"], names

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
meta = [v for k, v in blobs.items() if k.startswith("meta/user-123/")]
assert meta and meta[0][0] == "str"
record = json.loads(meta[0][1])
assert record["structData"]["user_id"] == "user-123", record
assert record["content"]["uri"].endswith("users/user-123/notes.md"), record
assert r.json()["documentId"] == record["id"]

print("ALL LOCAL TESTS PASSED")
