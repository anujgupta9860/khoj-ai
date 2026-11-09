"""Khoj A2A agent - conversational Q&A over a user's Khoj corpus.

Implements the A2A protocol (JSON-RPC 2.0): an AgentCard at
/.well-known/agent-card.json and message/send (+ message/stream) on the
root endpoint. Each request carries the caller's Khoj token as a
Bearer credential; the agent verifies it locally (shared secret,
app/auth.py), asks the web service for the caller's authorized corpus
(/api/agent/corpus — the web service enforces membership), searches
ONLY that corpus in OpenRAG (the single retrieval engine), and grounds
an LLM answer on the retrieved chunks. The agent holds no database or
storage credentials of its own.

This is the conversational surface for Khoj: the web app's chat panel
talks to this service as an A2A client, and any external A2A client can
do the same.
"""

import html
import io
import json
import os
import re
import time
import urllib.parse
import uuid
import zipfile

_TAG_RE = re.compile(r"<[^>]+>")


def clean_snippet(text: str) -> str:
    """Search snippets can carry highlight markup (<b>...</b>) and HTML
    entities (&nbsp;). Strip both so answers read as plain prose."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


_CITE_RE = re.compile(r"(?:\s*\[\d+\])(?:\s*,\s*\[\d+\])*")


def _strip_citations(text: str) -> str:
    """Answers cite the seeded excerpts as [1], [2]... - the markers
    are scaffolding for grounding, not reading material. The sources
    still travel separately in the response metadata."""
    return _CITE_RE.sub("", text or "")

import requests
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import verify_token

# The web service owns accounts, corpora and stored originals; this
# agent asks it (with the caller's own token) instead of holding
# database/storage credentials.
WEB_URL = os.environ.get("KHOJ_WEB_URL", "http://khoj").rstrip("/")
# OpenRAG is the single retrieval engine (in-cluster service).
OPENRAG_URL = os.environ.get("OPENRAG_URL", "http://openrag-backend:8000")
# NOTE: the OpenRAG backend serves UNPREFIXED routes (/search, /files,
# /router/upload_ingest, /tasks/...). The /api/* prefix exists only on the
# OpenRAG frontend's proxy (frontend/app/api/[...path] strips it). Calling
# the backend with /api/* returns 404 - learned live 2026-11-03.
# Answer model: OpenAI (the same provider OpenRAG itself runs on).
# Without a key the agent answers extractively from the source files.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# Optional custom OpenAI-compatible endpoint (proxy / gateway /
# other provider). Blank or unset = the default OpenAI API base.
OPENAI_BASE_URL = (
    os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
).rstrip("/")


PUBLIC_URL = os.environ.get("AGENT_PUBLIC_URL", "")

app = FastAPI(title="Khoj A2A Agent")

_history: dict[str, list] = {}  # contextId -> [{q, a}] (POC-scale, in-memory)


def require_user(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise PermissionError("Missing token")
    try:
        user = verify_token(authorization.split(" ", 1)[1])
    except Exception:
        raise PermissionError("Invalid token")
    user["_auth"] = authorization
    return user


def _openrag_chunks(query: str, filenames: list, limit: int = 30) -> list:
    resp = requests.post(
        f"{OPENRAG_URL}/search",
        json={"query": query, "filters": {"filename": filenames}, "limit": limit},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Search backend: {resp.text[:200]}")
    data = resp.json()
    return data if isinstance(data, list) else data.get("results", [])


def rewrite_query(question: str, history: list) -> str | None:
    """Turn the user's message into a standalone keyword search query
    using the model (OpenRAG's own chat does the same via its agent's
    tool calls; a single raw conversational message embeds poorly).
    Returns None when no model is available or the rewrite looks
    unusable - callers then search with the raw message only."""
    if not OPENAI_API_KEY:
        return None
    convo = "\n".join(f"User: {h['q']}\nAssistant: {h['a']}" for h in history[-2:])
    prompt = (
        "Rewrite the user's latest message as ONE standalone keyword "
        "search query for a document search index. Include the concrete "
        "nouns, names, form/field labels and synonyms that the matching "
        "passage would use. Use the conversation only to resolve "
        "references. Output ONLY the query text - no quotes, no "
        "explanation.\n\n"
        f"Conversation:\n{convo or '(none)'}\n\nLatest message: {question}"
    )
    try:
        resp = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            json={
                "model": OPENAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if not text or len(text) > 200 or "[" in text or "\n" in text:
            return None
        return text
    except Exception as e:
        print(f"rewrite_query: skipped: {e!r}", flush=True)
        return None



def _corpus_names(user: dict, org_id: str | None) -> dict:
    """OpenRAG filename -> (display title, storage scope prefix, docId)
    for every document the caller may search: their scope's uploads
    plus the global corpus. This map IS the corpus boundary - retrieval
    code post-filters every chunk against it.

    The list comes from the web service (GET /api/agent/corpus), which
    enforces membership/approval itself; an authorization failure there
    raises PermissionError here, exactly like the old direct checks."""
    params = {"orgId": org_id} if org_id else {}
    try:
        resp = requests.get(
            f"{WEB_URL}/api/agent/corpus",
            params=params,
            headers={"Authorization": user.get("_auth", "")},
            timeout=15,
        )
    except Exception:
        return {}
    if resp.status_code in (401, 403, 404):
        try:
            detail = resp.json().get("detail", "Not authorized for this corpus")
        except Exception:
            detail = "Not authorized for this corpus"
        raise PermissionError(detail)
    if resp.status_code != 200:
        return {}
    by_name = {}
    for f in resp.json().get("files", []):
        if f.get("openragName") and f.get("docId"):
            by_name[f["openragName"]] = (
                f.get("filename", ""), f.get("scope", ""), f["docId"],
            )
    return by_name


def _tool_search(query: str, user: dict, org_id: str | None, full_text_seen: set):
    """The search_documents tool: one scoped OpenRAG search, grouped
    by document. A small document is returned as full text the first
    time it surfaces (form-like documents chunk badly); later rounds
    return its chunks. Returns (result_text, [titles])."""
    by_name = _corpus_names(user, org_id)
    if not by_name:
        return "No documents are available to search.", []
    grouped: dict = {}
    for chunk in _openrag_chunks(query, sorted(by_name), limit=30):
        name = chunk.get("filename", "")
        if name not in by_name:
            continue  # another corpus's document - never used
        text = clean_snippet(chunk.get("text", ""))
        if not text:
            continue
        title, prefix, doc_id = by_name[name]
        entry = grouped.setdefault(
            name, {"title": title, "prefix": prefix, "docId": doc_id,
                   "snips": [], "score": 0}
        )
        entry["score"] = max(entry["score"], chunk.get("score", 0))
        if len(entry["snips"]) < 6:
            entry["snips"].append(text)
    if not grouped:
        return "No matching passages found.", []
    blocks, titles = [], []
    for name, entry in sorted(grouped.items(), key=lambda kv: -kv[1]["score"])[:4]:
        titles.append(entry["title"])
        full = None
        if name not in full_text_seen:
            try:
                full = fetch_document_text(entry["docId"], entry["title"], user)
            except Exception:
                full = None
            if full and len(full) <= 20000:
                full_text_seen.add(name)
                blocks.append(f"[{entry['title']} - full document]\n{full}")
                continue
        blocks.append(f"[{entry['title']}]\n" + "\n".join(entry["snips"]))
    return "\n\n".join(blocks)[:16000], titles


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Search the user's documents for passages relevant to a "
                "keyword query. Use it when the excerpts already provided "
                "are not enough - for example to find specific fields, "
                "labels, codes or amounts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword search query",
                    }
                },
                "required": ["query"],
            },
        },
    }
]


def _chat_completion(messages: list, tools: list | None = None) -> dict:
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.2}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    resp = requests.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=45,
    )
    if resp.status_code != 200:
        raise RuntimeError(resp.text[:200])
    return resp.json()["choices"][0]["message"]

def search_snippets(queries: list, user: dict, org_id: str | None, page_size: int = 6) -> list:
    """OpenRAG chunk search over ONLY the caller's corpus: every query
    is server-side filtered to the scope's document names (each name
    carries its doc-id prefix), then post-filtered again here. Chunks
    from all queries are merged (deduped, best score kept) so a
    reformulated query and the raw message both contribute."""
    by_name = _corpus_names(user, org_id)
    if not by_name:
        return []
    merged: dict = {}  # (openrag name, text head) -> (score, name, text)
    for query in queries:
        for chunk in _openrag_chunks(query, sorted(by_name)):
            name = chunk.get("filename", "")
            if name not in by_name:
                continue  # another corpus's document - never used for grounding
            text = clean_snippet(chunk.get("text", ""))
            if not text:
                continue
            key = (name, text[:160])
            score = chunk.get("score", 0)
            if key not in merged or score > merged[key][0]:
                merged[key] = (score, name, text)
    grouped: dict[str, dict] = {}
    for score, name, text in sorted(merged.values(), key=lambda v: -v[0]):
        title, prefix, doc_id = by_name[name]
        entry = grouped.setdefault(
            name, {"title": title, "snippets": [], "scope": prefix,
                   "docId": doc_id, "_score": score}
        )
        entry["_score"] = max(entry["_score"], score)
        if len(entry["snippets"]) < 8:
            entry["snippets"].append(text)
    order = sorted(grouped, key=lambda n: -grouped[n]["_score"])
    out = []
    budget = 24  # total snippets across all documents in the answer context
    for name in order[:page_size]:
        entry = grouped[name]
        entry.pop("_score", None)
        if not entry["snippets"]:
            continue
        entry["snippets"] = entry["snippets"][:budget]
        budget -= len(entry["snippets"])
        out.append(entry)
        if budget <= 0:
            break
    # Form-like documents (W-2s, statements, receipts) chunk badly:
    # the chunks that rank highest are glossaries and field-label lists,
    # while the filled values sit in layout-scrambled chunks the model
    # cannot tie to their labels (diagnosed live 2026-11-03 on a real
    # W-2: 24 chunks, deduction labels present, model still refused).
    # When a document's full text is small enough, hand the model the
    # whole text so it can cross-reference labels and values itself.
    for entry in out[:3]:
        try:
            full = fetch_document_text(entry.get("docId", ""), entry["title"], user)
        except Exception:
            full = None
        if full and len(full) <= 20000:
            entry["snippets"] = [full]
    return out


_STOPWORDS = {
    "what", "all", "the", "and", "for", "are", "has", "have", "does", "did",
    "with", "from", "this", "that", "about", "into", "your", "their", "there",
    "which", "when", "where", "who", "how", "why", "can", "could", "would",
    "should", "tell", "show", "give", "list", "find", "much", "many",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]*[a-z0-9]|[a-z0-9]")
_TEXT_CACHE: dict = {}
_CACHE_LIMIT = 64


def _query_terms(question: str) -> set:
    terms = set()
    for w in _WORD_RE.findall((question or "").lower()):
        if len(w) < 3 or w in _STOPWORDS:
            continue
        terms.add(w)
        if w.endswith("s") and len(w) > 3:
            terms.add(w[:-1])
    return terms


def _score_text(text: str, terms: set) -> int:
    words = _WORD_RE.findall(text.lower())
    if not words or not terms:
        return 0
    present = set(words)
    matched = terms.intersection(present)
    hits = sum(1 for w in words if w in terms)
    return len(matched) * 12 + min(hits, 24)


def _extract_docx(raw: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return None
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = _TAG_RE.sub("", xml)
    return html.unescape(xml)


def _extract_pdf(raw: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None


def extract_text(filename: str, raw: bytes) -> str | None:
    """Extract searchable text from an uploaded file's bytes. Supported:
    plain text formats directly, HTML by block-aware tag stripping,
    DOCX via its XML (stdlib only), PDF via pypdf. Anything else returns
    None and the caller falls back to search snippets for that file."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".docx":
        return _extract_docx(raw)
    if ext == ".pdf":
        return _extract_pdf(raw)
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    if ext in (".html", ".htm"):
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>|<br\s*/?>", "\n", text)
        text = _TAG_RE.sub(" ", text)
        text = html.unescape(text)
    return text


def fetch_document_text(doc_id: str, filename: str, user: dict | None) -> str | None:
    """Fetch one of the caller's documents (by docId) from the web
    service and return its extracted text. The web service owns the
    storage backend (local disk or GCS) and re-checks the caller's
    access to this exact document, so the agent needs no storage
    credentials. A small in-process cache avoids refetching on every
    chat turn (a re-upload under the same name is picked up on pod
    restart)."""
    if not doc_id or not filename or not user:
        return None
    cache_key = doc_id
    if cache_key in _TEXT_CACHE:
        return _TEXT_CACHE[cache_key]
    try:
        resp = requests.get(
            f"{WEB_URL}/api/documents/{urllib.parse.quote(doc_id, safe='')}/raw",
            headers={"Authorization": user.get("_auth", "")},
            timeout=(5, 20),
        )
        if resp.status_code != 200 or len(resp.content) > 12_000_000:
            return None
        text = extract_text(filename, resp.content)
    except Exception as e:
        print(f"fetch_document_text({filename}): {e!r}", flush=True)
        return None
    if text:
        if len(_TEXT_CACHE) >= _CACHE_LIMIT:
            _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
        _TEXT_CACHE[cache_key] = text[:300_000]
    return text


def _looks_like_heading(block: str, next_block: str | None) -> bool:
    if len(block) > 90:
        return False
    if block.endswith(":"):
        return True
    letters = [c for c in block if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.75:
        return len(block) >= 3
    if next_block and len(next_block) > max(120, int(len(block) * 1.6)):
        return not block.endswith((".", ",", ";", "!", "?"))
    return False


def _sections(text: str) -> list[tuple[str, str]]:
    lines = [line.strip() for line in (text or "").splitlines()]
    blocks = [line for line in lines if line]
    if not blocks:
        return []
    sections: list[tuple[str, str]] = []
    heading = ""
    current: list[str] = []
    current_len = 0

    def flush():
        if current:
            sections.append((heading, "\n".join(current)))

    for index, block in enumerate(blocks):
        next_block = blocks[index + 1] if index + 1 < len(blocks) else None
        if _looks_like_heading(block, next_block) and current_len >= 30:
            flush()
            heading = block
            current = []
            current_len = 0
            continue
        if _looks_like_heading(block, next_block) and not current:
            heading = block
            continue
        current.append(block)
        current_len += len(block) + 1
        if current_len >= 1800:
            flush()
            heading = ""
            current = []
            current_len = 0
    flush()
    return sections


def pick_passages(text: str, question: str, budget: int = 2400) -> tuple[str, int]:
    """Return the most relevant complete section(s), not snippets.
    Sections are heading + following text, scored by distinct query
    terms and density. Returned in document order within budget."""
    terms = _query_terms(question)
    if not terms:
        return "", 0
    scored = []
    for pos, (heading, body) in enumerate(_sections(text)):
        full = (heading + "\n" + body).strip() if heading else body
        score = _score_text(full, terms)
        if score > 0:
            scored.append((pos, score, full))
    if not scored:
        return "", 0
    best = max(score for _, score, _ in scored)
    chosen = []
    used = 0
    for _, score, full in sorted(scored):
        if used and used + len(full) + 2 > budget:
            continue
        chosen.append(full)
        used += len(full) + 2
        if used >= budget:
            break
    return "\n".join(chosen), best


def extractive_answer(question: str, sources: list, user: dict | None = None) -> str:
    """Complete extractive answer: retrieve the actual source files
    and quote the best full sections. Earlier version stitched the search engine's
    ~200-character snippets (first document only, max 600 chars), which
    produced visibly unfinished answers. Last-resort fallback still
    uses snippets across sources if no file can be fetched/parsed."""
    candidates: list[tuple[int, int, str, str]] = []
    for index, source in enumerate(sources[:3]):
        text = fetch_document_text(source.get("docId", ""), source.get("title", ""), user)
        if not text:
            continue
        passages, best = pick_passages(text, question)
        if passages:
            candidates.append((index, best, source["title"], passages))
    chunks: list[str] = []
    if candidates:
        top_best = max(best for _, best, _, _ in candidates)
        for _, best, title, passages in sorted(candidates):
            if chunks and best < top_best * 0.5:
                continue
            chunks.append(f"From {title}:\n{passages}")
            if len(chunks) >= 2:
                break
    if not chunks:
        chunks = [
            f"From {s['title']}: " + " ".join(s["snippets"])[:800]
            for s in sources if s.get("snippets")
        ]
    if not chunks:
        return (
            "I found matching documents, but could not extract readable "
            "passages from them."
            "\n\n(chat is running in extractive mode right now)"
        )
    return "\n\n".join(chunks) + "\n\n(chat is running in extractive mode right now)"


_REFUSAL_MARKERS = (
    "do not contain",
    "does not contain",
    "don't contain",
    "not contain information",
    "couldn't find",
    "could not find",
    "no information about",
)


def _looks_like_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def model_answer(question: str, sources: list, history: list,
                 user: dict | None = None, org_id: str | None = None):
    """Ground an OpenAI answer on the caller's documents, the way
    OpenRAG's own chat does: the model starts from the retrieved
    excerpts and may call search_documents for more passages before
    answering (Khoj's corpus filtering is enforced inside the tool -
    it can only ever search the caller's own scope plus the global
    corpus). Returns (answer_text, extra_titles_from_tool_searches).
    Falls back to an extractive answer when no key is configured or
    the model calls fail, so chat never hard-fails."""
    if not sources and user is None:
        return (
            "I couldn't find anything about that in the selected documents. "
            "Try different words, or upload the document that covers it."
        ), []
    if not OPENAI_API_KEY:
        return extractive_answer(question, sources, user), []
    excerpts = "\n\n".join(
        f"[{i + 1}] {s['title']}\n" + "\n".join(s["snippets"])
        for i, s in enumerate(sources)
    )
    history_text = "\n".join(f"User: {h['q']}\nAssistant: {h['a']}" for h in history[-4:])
    prompt = (
        "You are Khoj, a document assistant. Answer the question using ONLY "
        "the document excerpts below and any passages you retrieve with "
        "search_documents. Cite excerpts inline like [1], [2]. "
        "Some excerpts are complete documents: read them in full before "
        "concluding anything is missing. Forms and statements often list "
        "field names and their amounts separately, sometimes in matching "
        "order - pair them up and answer from the values you find. If the "
        "excerpts are not enough, call search_documents with different "
        "keywords (field labels, codes, synonyms) before giving up. Only "
        "say the information is not there if it is truly absent from every "
        "excerpt and search result. Be concise and direct.\n\n"
        f"Conversation so far:\n{history_text or '(none)'}\n\n"
        f"Document excerpts:\n{excerpts or '(none yet - search first)'}\n\n"
        f"Question: {question}"
    )
    messages = [{"role": "user", "content": prompt}]
    extra_titles: list = []
    full_text_seen: set = set()
    text = ""
    try:
        for _ in range(3):
            msg = _chat_completion(messages, tools=_TOOLS if user else None)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                break
            messages.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": calls,
            })
            for call in calls:
                fn = call.get("function") or {}
                if fn.get("name") != "search_documents" or user is None:
                    result = "That tool is unavailable."
                else:
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    result, titles = _tool_search(
                        str(args.get("query") or question),
                        user, org_id, full_text_seen,
                    )
                    for t in titles:
                        if t not in extra_titles:
                            extra_titles.append(t)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result,
                })
        if not text:
            msg = _chat_completion(messages)
            text = (msg.get("content") or "").strip()
        if not text:
            raise RuntimeError("Model returned an empty answer")
        if _looks_like_refusal(text):
            # One firmer pass: gpt-4o-mini refuses too eagerly on
            # form-like documents whose values sit apart from their
            # labels, even when handed the full text (diagnosed live
            # 2026-11-03).
            messages.append({
                "role": "user",
                "content": (
                    "Re-read every excerpt and search result completely, "
                    "including any complete document text. Pair field-name "
                    "lists with amount lists (in order when the document "
                    "presents them that way) and answer with the specific "
                    "figures if they appear anywhere."
                ),
            })
            msg = _chat_completion(messages)
            retry = (msg.get("content") or "").strip()
            if retry and not _looks_like_refusal(retry):
                text = retry
        return text, extra_titles
    except Exception as e:
        print(f"model_answer: falling back to extractive mode: {e!r}", flush=True)
    return extractive_answer(question, sources, user), []


AGENT_CARD = {
    "name": "Khoj Document Assistant",
    "description": "Ask questions over your Khoj documents - answers are grounded in your corpus (or your business's corpus) with citations.",
    "url": PUBLIC_URL,
    "version": "1.0.0",
    "protocolVersion": "0.3.0",
    "capabilities": {"streaming": True, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "securitySchemes": {
        "khojBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    },
    "security": [{"khojBearer": []}],
    "skills": [
        {
            "id": "answer-questions",
            "name": "Answer questions over documents",
            "description": "Conversational Q&A grounded in the caller's Khoj corpus; pass metadata.orgId to ask over a business corpus.",
            "tags": ["documents", "rag", "search", "qa"],
            "examples": [
                "What does the manual say about installation?",
                "Summarize the billing policy.",
            ],
        }
    ],
}


@app.get("/.well-known/agent-card.json")
def agent_card():
    return AGENT_CARD


@app.get("/healthz")
def healthz():
    return {"ok": True}


SUMMARY_SYSTEM = (
    "You summarize a customer support chat for the support engineer who "
    "will pick up the ticket. The customer first tried to solve their "
    "problem in this self-service chat; the ticket is the escalation. "
    "Write a concise, factual summary in Markdown with these short "
    "sections: **Issue** (what the customer needs), **What was covered** "
    "(what the assistant already explained or suggested), **Still "
    "unresolved** (what remains open; say 'Nothing stated' if the "
    "conversation resolved it). Use only facts from the conversation — "
    "never invent steps, error messages, names, or product details."
)


def _summarize_transcript(transcript: list) -> str:
    lines = []
    total = 0
    for turn in transcript[-30:]:
        if not isinstance(turn, dict):
            continue
        role = "Customer" if turn.get("role") == "user" else "Assistant"
        text = " ".join(str(turn.get("text") or "").split())[:2000]
        if not text:
            continue
        lines.append(f"{role}: {text}")
        total += len(text)
        if total > 12000:
            break
    if not lines:
        raise ValueError("Transcript is empty")
    msg = _chat_completion([
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": "Conversation:\n\n" + "\n\n".join(lines)},
    ])
    summary = (msg.get("content") or "").strip()
    if not summary:
        raise RuntimeError("Empty summary from the model")
    return summary


@app.post("/summarize")
async def summarize(request: Request, authorization: str | None = Header(default=None)):
    """Summarize a chat transcript for a support ticket. Called by the
    web service when a user escalates a chat to Jira; same Khoj token
    auth as the A2A endpoint."""
    try:
        require_user(authorization)
    except Exception:
        return JSONResponse({"detail": "Unauthorized: valid Khoj token required"}, status_code=401)
    body = await request.json()
    transcript = body.get("transcript") or []
    if not isinstance(transcript, list) or not transcript:
        return JSONResponse({"detail": "transcript is required"}, status_code=422)
    if not OPENAI_API_KEY:
        return JSONResponse({"detail": "No answer model configured"}, status_code=501)
    try:
        summary = _summarize_transcript(transcript)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"detail": f"Summarization failed: {e}"}, status_code=502)
    return {"summary": summary}


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_message(params: dict, user: dict) -> dict:
    msg = params.get("message") or {}
    parts = msg.get("parts") or []
    text = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text").strip()
    if not text:
        raise ValueError("Message has no text part")
    context_id = msg.get("contextId") or uuid.uuid4().hex
    metadata = msg.get("metadata") or {}
    org_id = metadata.get("orgId") or None
    history = _history.setdefault(context_id, [])
    queries = []
    rewritten = rewrite_query(text, history)
    if rewritten and rewritten.lower() != text.lower():
        queries.append(rewritten)
    queries.append(text)
    sources = search_snippets(queries, user, org_id)
    answer, extra_titles = model_answer(text, sources, history, user, org_id)
    answer = _strip_citations(answer)
    source_titles = [s["title"] for s in sources]
    for t in extra_titles:
        if t not in source_titles:
            source_titles.append(t)
    history.append({"q": text, "a": answer})
    del history[:-6]
    return {
        "kind": "message",
        "messageId": uuid.uuid4().hex,
        "role": "agent",
        "contextId": context_id,
        "parts": [{"kind": "text", "text": answer}],
        "metadata": {"sources": source_titles},
    }


@app.post("/")
@app.post("/a2a")
async def a2a(request: Request, authorization: str | None = Header(default=None)):
    body = await request.json()
    req_id = body.get("id")
    method = body.get("method", "")
    try:
        user = require_user(authorization)
    except Exception:
        return JSONResponse(_rpc_error(req_id, -32001, "Unauthorized: valid Khoj token required"))
    if method == "message/send":
        try:
            result = _handle_message(body.get("params") or {}, user)
        except PermissionError as e:
            return JSONResponse(_rpc_error(req_id, -32003, str(e)))
        except ValueError as e:
            return JSONResponse(_rpc_error(req_id, -32602, str(e)))
        except Exception as e:  # backend failures surface as internal errors
            return JSONResponse(_rpc_error(req_id, -32603, str(e)[:300]))
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "message/stream":
        def events():
            import json as _json
            task = {
                "kind": "task", "id": uuid.uuid4().hex,
                "contextId": (body.get("params") or {}).get("message", {}).get("contextId") or uuid.uuid4().hex,
                "status": {"state": "working"},
            }
            yield f"data: {_json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'kind': 'statusUpdate', 'taskId': task['id'], 'contextId': task['contextId'], 'status': {'state': 'working'}, 'final': False}})}\n\n"
            try:
                result = _handle_message(body.get("params") or {}, user)
                yield f"data: {_json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': result})}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps(_rpc_error(req_id, -32603, str(e)[:300]))}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return JSONResponse(_rpc_error(req_id, -32601, f"Method not found: {method}"))
