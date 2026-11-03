"""Local wiring tests for the Khoj A2A agent. Fully offline: Firebase
token verification, the OpenRAG search call, and the OpenAI call are
stubbed. Run:  python agent/test_agent_local.py
"""

import os
import sys

os.environ.setdefault("GCP_PROJECT", "innovation-lab-2026")
os.environ.setdefault("KHOJ_BUCKET", "khoj-ai-corpus-innovation-lab")

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
agent_main.OPENAI_API_KEY = "test-key"

# the caller's corpus: one document, known to the upload records
agent_main._org_service.record_upload({
    "docId": "doc-test123",
    "scopeKey": "user:user-123",
    "uid": "user-123",
    "orgId": None,
    "filename": "doc.md",
    "openragTask": "task-0",
    "uploadedAt": 0,
})
OPENRAG_NAME = "doc-test123--doc.md"

# the global corpus (platform org documents) is searchable by everyone
agent_main._org_service.record_upload({
    "docId": "doc-global1",
    "scopeKey": "org:khoj",
    "uid": "admin-0",
    "orgId": "khoj",
    "filename": "handbook.md",
    "openragTask": "task-0",
    "uploadedAt": 0,
})
GLOBAL_NAME = "doc-global1--handbook.md"

calls = []


class Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)[:200]

    def json(self):
        return self._payload


RETRY_PROBE = {"armed": False, "answers": 0}


def fake_post(url, json=None, headers=None, timeout=None):
    calls.append((url, json))
    if "api.openai.com" in url:
        if "Rewrite the user's latest message" in (json or {}).get("messages", [{}])[0].get("content", ""):
            return Resp({"choices": [{"message": {"content": "pump warranty years coverage"}}]})
        if RETRY_PROBE["armed"]:
            RETRY_PROBE["answers"] += 1
            if RETRY_PROBE["answers"] == 1:
                return Resp({"choices": [{"message": {"content":
                    "The excerpts do not contain information about that."}}]})
            return Resp({"choices": [{"message": {"content":
                "Recovered answer with figures [1]."}}]})
        return Resp({"choices": [{"message": {"content": "Grounded answer [1]."}}]})
    # OpenRAG chunk search
    return Resp({
        "results": [
            {
                "filename": OPENRAG_NAME,
                "text": "The <b>pump</b> warranty is&nbsp;2 years.",
                "score": 3.5,
            },
            {
                "filename": GLOBAL_NAME,
                "text": "The global handbook says hello.",
                "score": 1.0,
            },
        ]
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
assert result["metadata"]["sources"] == ["doc.md", "handbook.md"]
assert result["contextId"]

# the OpenRAG search was filtered to exactly this corpus's documents,
# and the answer came from the configured OpenAI model
from urllib.parse import urlsplit
search_calls = [j for u, j in calls if urlsplit(u).path == "/search"]
# retrieval runs BOTH the model-rewritten query and the raw message, merged
search_queries = [j["query"] for j in search_calls]
assert "pump warranty years coverage" in search_queries, search_queries
assert "What is the warranty?" in search_queries, search_queries
assert search_calls and search_calls[-1]["filters"] == {
    "filename": sorted([OPENRAG_NAME, GLOBAL_NAME])}, search_calls
model_calls = [j for u, j in calls if "api.openai.com" in u]
assert model_calls and model_calls[-1]["model"] == "gpt-4o-mini", model_calls

# small documents are handed to the model as FULL TEXT, not just chunks
# (form-like documents chunk badly - glossaries outrank the filled values)
real_fetch = agent_main.fetch_document_text
agent_main.fetch_document_text = lambda scope, filename: (
    "FULLTEXT MARKER: the pump warranty is 2 years, parts and labour."
    if filename == "doc.md" else None
)
calls.clear()
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 30, "method": "message/send",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m-full",
        "parts": [{"kind": "text", "text": "What does the warranty cover?"}],
    }},
}, headers=H)
assert r.status_code == 200, r.text
answer_prompts = [
    j["messages"][0]["content"] for u, j in calls
    if "api.openai.com" in u and "Document excerpts" in j["messages"][0]["content"]
]
assert answer_prompts and "FULLTEXT MARKER" in answer_prompts[-1], answer_prompts
agent_main.fetch_document_text = real_fetch
calls.clear()

# a first-pass refusal triggers ONE firmer retry, whose answer wins
RETRY_PROBE["armed"] = True
calls.clear()
r = client.post("/", json={
    "jsonrpc": "2.0", "id": 31, "method": "message/send",
    "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m-retry",
        "parts": [{"kind": "text", "text": "What are the deduction amounts?"}],
    }},
}, headers=H)
assert r.status_code == 200, r.text
probe_text = r.json()["result"]["parts"][0]["text"]
assert "Recovered answer with figures" in probe_text, probe_text
assert RETRY_PROBE["answers"] == 2, RETRY_PROBE
RETRY_PROBE["armed"] = False
calls.clear()

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
# highlight markup and HTML entities before reaching the user
def fake_post_no_model(url, json=None, headers=None, timeout=None):
    if "api.openai.com" in url:
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

# --- Complete extractive passages (no LLM) ---
import io
import zipfile

docx_buffer = io.BytesIO()
with zipfile.ZipFile(docx_buffer, "w") as zf:
    zf.writestr(
        "word/document.xml",
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p><w:r><w:t>Anuj Gupta</w:t></w:r></w:p>
            <w:p><w:r><w:t>TECHNICAL SKILLS</w:t></w:r></w:p>
            <w:p><w:r><w:t>Java / J2EE, Spring Boot, Python, SQL, Docker, Kubernetes, and distributed systems design for resilient production services.</w:t></w:r></w:p>
            <w:p><w:r><w:t>EXPERIENCE</w:t></w:r></w:p>
            <w:p><w:r><w:t>Built payment services and internal engineering platforms.</w:t></w:r></w:p>
          </w:body>
        </w:document>""",
    )
search_sources = agent_main.search_snippets("warranty?", {"uid": "user-123"}, None)
assert search_sources[0]["scope"] == "users/user-123", search_sources
assert search_sources[0]["title"] == "doc.md", search_sources
assert search_sources[1]["title"] == "handbook.md", search_sources  # global corpus
assert search_sources[1]["scope"] == "orgs/khoj", search_sources

resume_text = agent_main.extract_text("anujResume_2024.docx", docx_buffer.getvalue())
assert "TECHNICAL SKILLS" in resume_text
assert "distributed systems design for resilient production services" in resume_text
passages, best = agent_main.pick_passages(resume_text, "What all skill Anuj Gupta has")
assert best > 0
assert "TECHNICAL SKILLS" in passages
assert "distributed systems design for resilient production services" in passages

original_fetch = agent_main.fetch_document_text
agent_main.fetch_document_text = lambda scope, filename: resume_text if filename.endswith(".docx") else None
full_answer = agent_main.extractive_answer(
    "What all skill Anuj Gupta has",
    [{"title": "anujResume_2024.docx", "snippets": ["partial"], "scope": "user-123"}],
)
assert "distributed systems design for resilient production services" in full_answer
assert "extractive mode" in full_answer
agent_main.fetch_document_text = original_fetch

# no API key configured -> the model path is skipped, extractive answers
agent_main.OPENAI_API_KEY = ""
ans = agent_main.model_answer(
    "warranty?",
    [{"title": "doc.md", "snippets": ["The pump warranty is 2 years."], "scope": "users/user-123"}],
    [],
)
assert "extractive mode" in ans, ans
agent_main.OPENAI_API_KEY = "test-key"

print("ALL AGENT TESTS PASSED")
