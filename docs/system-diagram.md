# Khoj.ai — System Diagram (single-stack, OpenRAG)

*As of Nov 3, 2026 — single-stack OpenRAG build `3eed4af` live. GCP project `innovation-lab-2026`.*

```mermaid
flowchart TB
    subgraph Clients
        U["User browser / iPhone Safari"]
        MCP["MCP client<br/>(Claude / Cursor)"]
        A2A["External A2A client"]
    end

    subgraph GKE["GKE Autopilot cluster khoj — us-central1 (regional), v1.36.4"]
        subgraph KhojNS["Khoj product"]
            WEB["khoj (web + API + MCP)<br/>FastAPI · Helm charts/khoj<br/>Ingress 34.8.195.76"]
            AGENT["khoj-agent (A2A)<br/>AgentCard + message/send + stream<br/>cluster-internal only"]
        end
        subgraph OpenRAGNS["OpenRAG stack — the single retrieval engine"]
            ORFE["openrag-frontend<br/>34.102.202.103"]
            ORBE["openrag-backend<br/>openrag-backend:8000 in-cluster"]
            LF["langflow<br/>136.68.156.112"]
            OS["opensearch 3.8 (jvector)<br/>documents index"]
            OSD["opensearch-dashboards 3.0<br/>8.233.7.242"]
            DOC["docling<br/>cluster-internal docling:5001"]
        end
        ESO["External Secrets Operator 2.14<br/>ClusterSecretStore gcpsm"]
    end

    FB["Built-in auth<br/>email/password · app-issued JWT on every call"]
    FS[("SQLite (khoj.db)<br/>users · orgs · memberships · invites<br/>uploads · events")]
    GCS[("GCS bucket khoj-ai-corpus-innovation-lab<br/>original files: users/{uid}/… · orgs/{orgId}/…<br/>cold copies / re-ingestion source")]
    GSM[("Google Secret Manager<br/>openrag-openai-api-key (shared)<br/>opensearch / langflow / session secrets")]
    OAI["OpenAI API<br/>gpt-4o-mini (chat + answers)<br/>text-embedding-3-small (embeddings)"]
    CB["Cloud Build (cloudbuild.yaml)<br/>builds khoj + khoj-agent + patched<br/>openrag-backend → Artifact Registry → helm upgrade"]

    U --> WEB
    MCP -->|POST /mcp, Bearer = Firebase ID token| WEB
    A2A --> AGENT
    WEB -->|A2A message/send, caller token forwarded| AGENT
    WEB -->|verify ID token| FB
    AGENT -->|verify ID token| FB
    WEB --> FS
    AGENT --> FS
    WEB -->|keep original| GCS
    AGENT -.->|extractive fallback: fetch source file| GCS
    WEB -->|upload_ingest · search (filename-filtered)| ORBE
    AGENT -->|search (filename-filtered)| ORBE
    AGENT -->|answer with gpt-4o-mini| OAI
    ORFE --> ORBE
    ORBE --> OS
    ORBE --> LF
    ORBE --> DOC
    LF --> DOC
    OSD --> OS
    ORBE -->|embeddings + chat LLM| OAI
    ESO -->|sync secrets| GSM
    ESO -.->|OPENAI_API_KEY| AGENT
    ESO -.->|OPENAI_API_KEY + passwords| ORBE
    CB -.->|deploy| GKE
```

## How a document flows

1. **Upload (Khoj UI):** file → GCS original (`users/{uid}/` or `orgs/{orgId}/`) → forwarded to OpenRAG `POST /router/upload_ingest` (in-cluster route, unprefixed) under the name `<docId>--<filename>` → Docling parses (OCR enabled) → Langflow ingestion flow → chunks + embeddings in OpenSearch. Khoj also registers a **knowledge filter** for the document (`POST /knowledge-filter`, data_sources = [the document]) so uploads land in OpenRAG's Knowledge tab — that tab lists filters, not files. Status in Khoj flips `indexing → ready` when the name appears in OpenRAG `GET /files`.
2. **Search:** Khoj reads the corpus's upload records (SQLite), then `POST /search` to OpenRAG with a server-side `filename` filter naming exactly that corpus's documents **plus the global corpus** (the platform org Khoj.ai's documents, searchable by everyone), plus a post-filter, plus a **relevance floor** (`KHOJ_MIN_SCORE`, default 1.5 — measured junk baseline is 1.25–1.29, genuine matches 1.65+), so unrelated queries return nothing instead of the caller's nearest documents. Another business's documents are never in the filter, so they are unreachable.
3. **Chat:** web → A2A `message/send` to khoj-agent → seeded scoped retrieval (query rewrite + raw message, merged) → OpenAI `gpt-4o-mini` — the same model OpenRAG's own chat is configured with — answers in an **OpenRAG-style tool loop**: it may call a `search_documents` tool (up to 3 rounds, same corpus filter enforced inside the tool) before answering. Small documents (≤20k chars) are handed to the model as full text, because form-like PDFs chunk into glossaries that outrank the filled values; a first-pass refusal triggers one firmer re-read. If the model call fails entirely, extractive answers quote full sections of the GCS originals.
4. **Isolation:** docId = `doc-` + sha256(`{scope}/{filename}`)[:24] — two users can both upload `resume.pdf`; their OpenRAG names never collide.

## Infra details

| Piece | Detail |
|---|---|
| Cluster | GKE Autopilot `khoj`, us-central1 regional, endpoint 136.64.172.159, v1.36.4-gke.2046003 |
| Public endpoints | Khoj http://34.8.195.76 · OpenRAG http://34.102.202.103 · Langflow http://136.68.156.112 · Dashboards http://8.233.7.242 (all HTTP, no domain yet) |
| In-cluster | `openrag-backend:8000` (Khoj's `OPENRAG_URL`), `khoj-agent:80`, `docling:5001`, `opensearch:9200` |
| Identity | Built-in auth (SQLite accounts, app-issued JWTs, shared signing secret). Workload Identity only when storage backend = gcs (bucket access) |
| Secrets | GSM → ESO (`external-secrets.io/v1`, ClusterSecretStore `gcpsm`) → k8s Secrets. No secrets in git/values. OpenAI key shared: GSM `openrag-openai-api-key` |
| Data | SQLite (accounts, app state, upload records, logs) · originals on local disk or GCS bucket (KHOJ_STORAGE) · OpenSearch `documents` index (chunks/embeddings) |
| Pipeline | `cloudbuild.yaml`: build 3 images → push to Artifact Registry in-step → token kubeconfig → `helm upgrade khoj` + `khoj-agent`. Trigger: manual submit (GitHub trigger pending console OAuth) |
| Single stack (Nov 3, 2026) | OpenRAG on GKE is the only retrieval stack; the earlier OpenRAG VM (`openrag-1`) was deleted Nov 3 — GKE is the only deployment. |
