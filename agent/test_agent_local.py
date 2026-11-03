"""Local wiring tests for the Khoj A2A agent. Fully offline: Firebase
token verification, the Vertex search call, and the Gemini call are
stubbed. Run:  python agent/test_agent_local.py
"""

import os
import sys

os.environ.setdefault("GCP_PROJECT", "innovation-lab-2026")
os.environ.setdefault("KHOJ_ENGINE_ID", "khoj-search")
os.environ.setdefault("KHOJ_DATASTORE_ID", "khoj-corpus_test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import firebase_admin

firebase_admin.initialize_app = lambda *a, **k: None

from fastapi.testclient import TestClient  # noqa: E402

import agent.main as agent_main  # noqa: E402
from app.orgs import InMemoryOrgService  # noqa: E402

agent_main.fb_auth.verify_id_token = lambda token: {
    "uid": "user-123", "email": "u@example.com",
}
agent_main._org_service = InMemoryOrgService()
agent_main.gcp_token = lambda: "fake-token"

calls = []


class Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)[:200]

    def json(self):
        return self._payload


def fake_post(url, json=None, headers=None, timeout=None):
    calls.append(url)
    if "aiplatform" in url:
        return Resp({"candidates": [{"content": {"parts": [{"text": "Grounded answer [1]."}]}}]})
    return Resp({
        "results": [{
            "document": {
                "id": "d1",
                "structData": {"title": "doc.md", "user_id": "user-123"},
                "derivedStructData": {"snippets": [{"snippet": "The <b>pump</b> warranty is&nbsp;2 years."}]},
            }
        }]
    })


agent_main.requests.post = fake_post
client = TestClient(agent_main.app, raise_server_exceptions=False)
H = {"Authorization": "Bearer whatever"}

# AgentCard discovery
r = client.get("/.well-known/agent-card.json")
assert r.status_code == 200, r.text
card = r.json()
assert card["name"] == "Khoj Document Assistant"
assert card["skills"][0]["id"] == "answer-questions"
assert card["capabilities"]["streaming"] is True

# message/send without a token -> JSON-RPC auth error
r = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                           "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}})
assert r.json()["error"]["code"] == -32001, r.json()

# message/send happy path: grounded answer + sources + context
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 2, "method": "message/send",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m1",
        "parts": [{"kind": "text", "text": "What is the warranty?"}],
    }},
}, headers=H)
assert r.status_code == 200, r.text
result = r.json()["result"]
assert result["kind"] == "message" and result["role"] == "agent"
assert "Grounded answer" in result["parts"][0]["text"]
assert result["metadata"]["sources"] == ["doc.md"]
assert result["contextId"]

# same context keeps history (second turn answers too)
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 3, "method": "message/send",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m2",
        "contextId": result["contextId"],
        "parts": [{"kind": "text", "text": "And for the pro model?"}],
    }},
}, headers=H)
assert r.json()["result"]["contextId"] == result["contextId"]

# orgId for a business the caller does not belong to -> forbidden error
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 4, "method": "message/send",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m3",
        "parts": [{"kind": "text", "text": "q"}],
        "metadata": {"orgId": "org-nope"},
    }},
}, headers=H)
assert r.json()["error"]["code"] == -32003, r.json()

# unknown method
r = client.post("/", json={"jsonrpc": "2.0", "id": 5, "method": "tasks/get", "params": {}}, headers=H)
assert r.json()["error"]["code"] == -32601

# streaming variant yields SSE events ending in the reply message
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 6, "method": "message/stream",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m4",
        "parts": [{"kind": "text", "text": "warranty?"}],
    }},
}, headers=H)
assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
assert "Grounded answer" in r.text

# extractive fallback (model call fails): snippets are cleaned of
# Vertex highlight markup and HTML entities before reaching the user
def fake_post_no_model(url, json=None, headers=None, timeout=None):
    if "aiplatform" in url:
        return Resp({"error": "unavailable"}, status=503)
    return fake_post(url, json=json, headers=headers, timeout=timeout)


agent_main.requests.post = fake_post_no_model
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 7, "method": "message/send",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m5",
        "parts": [{"kind": "text", "text": "warranty?"}],
    }},
}, headers=H)
text = r.json()["result"]["parts"][0]["text"]
assert "The pump warranty is 2 years." in text, text
assert "<b>" not in text and "&nbsp;" not in text, text
assert "extractive mode" in text
agent_main.requests.post = fake_post

# --- Gemini endpoint selection (API key route vs Vertex route) ---
agent_main.GEMINI_API_KEY = ""
url, headers = agent_main.gemini_endpoint()
assert "aiplatform.googleapis.com" in url and "Authorization" in headers, (url, headers)
agent_main.GEMINI_API_KEY = "test-key-123"
url, headers = agent_main.gemini_endpoint()
assert url == ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-2.5-flash:generateContent"), url
assert headers == {"x-goog-api-key": "test-key-123"}, headers
agent_main.GEMINI_API_KEY = ""

print("ALL AGENT TESTS PASSED")
