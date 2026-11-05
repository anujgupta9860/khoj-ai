# Super Admin Metrics — Sources and Formulas

*Every number on the Super Admin page, traced to the API or record it comes from and the
exact arithmetic behind it. Code: `app/main.py` (`admin_metrics`, `admin_conversations`,
`admin_file_stats`, `_openrag_files`, and the `log_event` calls in the search/chat
handlers); rendering in `app/static/index.html` (`loadAdmin`).*

## The one-line version

Only **one OpenRAG API** feeds the whole page: **`GET /files`** (unprefixed,
`http://openrag-backend:8000/files`). Everything else is Khoj's own records in SQLite (via the org service)
(`file_uploads`, `search_logs`, `orgs`) plus arithmetic. No OpenRAG metrics, task, or
stats endpoint is used — OpenRAG exposes none that fit.

## Data sources

| Source | What it provides | How Khoj gets it |
|---|---|---|
| OpenRAG `GET /files` | Per file: `chunks` (chunk count), `indexed_time` (when indexing finished) | `_openrag_files()` — keyed by the scoped OpenRAG filename (`{doc_id}--{filename}`), cached 5 s |
| SQLite `uploads` | Per upload: `docId`, `filename`, `uid`, `orgId`, `uploadedAt` (epoch s) | `org_service().list_all_uploads()` |
| SQLite `events` | Every search/chat/upload event: `kind`, `query`, `answer`, `results`, `latencyMs`, `source`, `ts`, `email`, `orgId` | `org_service().list_events(limit)` |
| SQLite `orgs` | Org status counts (approved / pending / rejected) | `admin_metrics` |

## The file matrix (Files — ingestion time & chunks)

Served by `GET /api/admin/file-stats`, grouped **global** (platform org) → **per org** →
**personal** (by uid), newest first. Per file:

| Column | Formula |
|---|---|
| **File** | `file_uploads.filename` |
| **Chunks** | `chunks` from OpenRAG `GET /files` for the file's scoped name — i.e. how many chunk documents that filename has in the OpenSearch `documents` index |
| **Ingestion** | `indexed_time − uploadedAt`, in seconds, rounded to 0.1. `indexed_time` is parsed defensively (epoch s, epoch ms, or ISO string). Shown as "—" if either timestamp is missing or the index time precedes the upload time |
| **Uploaded** | `file_uploads.uploadedAt` |
| **Status** | `ready` if the file appears in `GET /files` at all; otherwise `indexing` |

So "ingestion time" measures the full pipeline a user experiences — from the moment
Khoj accepted the upload (original stored + record written, then forwarding) until OpenRAG
finished converting, chunking, embedding and indexing it (Docling + Langflow + OpenAI
embeddings + OpenSearch write all included).

## Questions & answers (all users)

Served by `GET /api/admin/conversations`, read from `search_logs` (latest 500 events,
chat + search kinds):

| Field | Source |
|---|---|
| Question | The logged `query` |
| Answer | The logged `answer` — `/api/chat` stores the **full answer text** (a change made with this page; older events hold only a 500-char summary) |
| Who / where | Logged `email`, and the org name resolved from `orgId` ("Personal" when none) |
| **Answered in X** | The event's `latencyMs` (below) |
| Sources | Count of the sources the agent's A2A response metadata returned |

## How the latencies are measured ("till the answer")

Both latencies are **wall-clock, end-to-end**, measured with `time.monotonic()` in the
web service and stored on the log event at write time:

- **Search latency** — `started` is taken at the top of the `/api/search` handler; the
  clock stops when the result is logged. The window covers: the OpenRAG
  `POST /search` call, plus Khoj's post-processing (relevance floor `KHOJ_MIN_SCORE`,
  shell-hit drop, scope post-filter).
- **Chat answer latency ("answered in X")** — `started` is taken at the top of the
  `/api/chat` handler; the clock stops after the agent's reply arrives and is logged.
  The window covers the **entire round trip**: web → `khoj-agent` (A2A `message/send`)
  → the agent's seeded OpenRAG searches + up to 3 `search_documents` tool rounds →
  gpt-4o-mini generation → response back to the web service. It is the time the user
  actually waited, not an OpenRAG-internal timing.

## The tiles on top

| Tile | Formula |
|---|---|
| Businesses approved / pending requests | Org counts by status (SQLite `orgs`) |
| Searches (all time) | Count of `kind="search"` events |
| Files | Count of all entries across the file-stats groups |
| Chunks total | Σ `chunks` over those files (from `GET /files`) |
| Avg ingestion time | Mean of the non-null per-file ingestion seconds |
| Avg chat answer time | Mean of `latencyMs` over the chat entries in the conversations payload |
| Zero-result rate | Share of search events with `results == 0` |

Sub-line under the tiles (from `/api/admin/metrics`): searches in the last 7 days,
upload count, unique users, **avg search latency** (mean of search events' `latencyMs`),
and the MCP-vs-web split (the events' `source` field).

## What is *not* measured

- No per-stage breakdown inside ingestion (Docling vs embedding vs indexing) — OpenRAG
  does not report stage timings on `/files`; only the upload→indexed total is knowable
  from outside.
- No token counts or model costs — the agent does not log them today.
- Averages are plain means over what is logged (searches: up to the latest 1,000
  events; conversations: latest 500), not time-windowed aggregates.
