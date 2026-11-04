# How OpenRAG Works

*The engine on its own — components and the two flows (ingestion and asking) that do all the
work. Based on the upstream `langflow-ai/openrag` source and the GKE deployment verified
Nov 3, 2026.*

## Diagram

![How OpenRAG works](diagrams/openrag-how-it-works.png)

```mermaid
flowchart TB
    subgraph Clients["Clients"]
        UI["OpenRAG Web UI (Next.js)<br/>Chat · Knowledge · Settings<br/>proxies /api/* to the backend"]
        API["API clients<br/>Khoj, curl, other apps"]
    end

    subgraph Backend["OpenRAG Backend (FastAPI)"]
        RT["Routes<br/>/router/upload_ingest · /files · /search<br/>/chat · /tasks · /knowledge-filter"]
        CFG[("Workspace config (SQLite)<br/>provider, models, chunking, OCR")]
        SVC["Services<br/>ingestion tasks · search service<br/>chat service · knowledge filters"]
    end

    subgraph Processing["Processing"]
        DOC["Docling<br/>converts PDF / Office / images<br/>to structured text (OCR)"]
        LF["Langflow flows<br/>ingestion flow: split + embed<br/>agent flow: LLM + search tool"]
    end

    subgraph Store["Storage — OpenSearch"]
        IDX[("documents index<br/>chunks + embedding vectors<br/>vector (kNN) + keyword search")]
        KF[("knowledge_filters index<br/>saved scopes over files")]
        DASH["OpenSearch Dashboards<br/>inspect the index"]
    end

    subgraph Providers["Model providers (configurable)"]
        LLM["Chat LLM<br/>gpt-4o-mini in this deployment"]
        EMB["Embedding model<br/>text-embedding-3-small"]
    end

    UI -->|"/api/* (prefix stripped)"| RT
    API --> RT
    RT --- SVC
    RT --- CFG

    SVC -->|"1 · file to convert (async)"| DOC
    DOC -->|"structured text"| SVC
    SVC -->|"2 · run ingestion flow"| LF
    LF -->|"embed chunks"| EMB
    LF -->|"3 · write chunks + vectors"| IDX

    SVC -->|"4 · search: embed the query"| EMB
    SVC -->|"5 · retrieve top chunks"| IDX
    SVC -->|"6 · chat: run agent flow"| LF
    LF -->|"decide + call search tool"| SVC
    LF -->|"write the grounded answer"| LLM
    SVC -.->|"knowledge filter = saved file scope"| KF
    IDX --- DASH
```

## The pieces

| Piece | What it does |
|---|---|
| **Web UI** | Next.js app with Chat, Knowledge and Settings pages. It owns no logic of its own for data: every call goes through its `/api/*` proxy to the backend, which strips the prefix |
| **Backend** | FastAPI service holding all routes and orchestration. Configuration (provider, chat/embedding models, chunk size 1000 / overlap 200, OCR on/off) lives in a small SQLite database; long work (ingestion) runs as tracked **tasks** |
| **Docling** | The parser. Converts PDFs, Office files and images into structured text/markdown, with OCR for scans. Conversion is asynchronous — the backend submits and polls |
| **Langflow** | The flow engine. Two flows matter: the **ingestion flow** (split text into chunks, embed them) and the **agent flow** (the chat brain: an LLM equipped with a search tool). Flows are stored in Langflow's own database and are *locked* against casual editing |
| **OpenSearch** | The store and the retriever. The `documents` index holds every chunk's text plus its embedding vector, and answers both vector (kNN, jvector engine) and keyword queries — the returned chunk score blends the two. A second index holds **knowledge filters** |
| **Providers** | Pluggable model backends (OpenAI, Anthropic, Ollama, WatsonX, custom endpoints). One provider supplies the chat LLM, another (or the same) the embedding model; a health endpoint validates both |

## Flow 1 — ingestion (a file becomes searchable)

1. A file arrives at `POST /router/upload_ingest`; the backend creates an ingestion **task**
   and returns its id immediately.
2. The backend hands the file to **Docling**, which converts it to structured text (OCR if
   enabled) — asynchronously, polled to completion.
3. The **Langflow ingestion flow** splits the text into chunks (configured size/overlap) and
   calls the **embedding model** to vectorize each chunk.
4. Chunks + vectors are written into the OpenSearch `documents` index under the file's name.
5. The task completes; the file now appears in `GET /files` with its chunk count and indexed
   time, and — if a knowledge filter names it — in the Knowledge tab.

## Flow 2 — asking (search and chat)

**Search** (`POST /search`) is the atomic operation everything else is built on:
the query is embedded with the same embedding model, OpenSearch retrieves the top chunks by
blended vector + keyword score, and the chunks come back with their scores and source
filenames. Optional filters narrow the search — most importantly by filename, or by a
**knowledge filter** (a named, saved set of files: "chat only over these documents").

**Chat** (`POST /chat`) wraps search in an agent loop:
1. The backend's chat service starts the **Langflow agent flow** with the question.
2. The **LLM** reads the question and calls its **search tool** — one or more `/search`
   retrievals, scoped by the selected knowledge filter if there is one.
3. Retrieved chunks are handed back to the LLM, which writes a **grounded answer** citing
   the source documents; the answer streams back to the UI and the conversation is saved.

That split — a dumb, reliable `/search` underneath a smart, iterative chat loop — is the
whole architecture. Anything that only needs retrieval (like Khoj) can live on `/search`
alone; anything that needs answers uses the agent flow on top of it.
