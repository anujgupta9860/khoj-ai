# Khoj.ai

Hosted document search, primarily for organizations, with a direct
individual path too. Runs anywhere containers run — no cloud account
required (see ADR-013 in docs/sdr-adr.md).

- **Khoj for businesses (primary):** an org admin enrolls the business,
  sets its branding, and invites employees by email (in-app accept or a
  one-time invite link). Admins upload documents into the shared
  business corpus; every member can search it.
- **Khoj for individuals (direct):** anyone can sign up directly, upload
  their own documents, and search only those.
- Both flavors also work from inside the user's own agent via the MCP
  server.

Every document is stored in OpenRAG under a name derived from its
scope + filename (`<docId>--<filename>`, docId = sha256 of
`{scope}/{filename}`), and every search is filtered server-side to
exactly the document names of a corpus the verified caller belongs
to — businesses by `org_id`, personal corpora by account id. The
platform org's corpus is the **global corpus**: searchable by every
signed-in user and every business, alongside their own documents —
never another business's.

- **Platform / super admin:** Khoj.ai itself is the platform org (id
  `khoj`). Its admins are super admins: org signups start **pending**
  and a super admin approves or rejects them (a pending org's admin can
  brand and invite, but its corpus stays closed). Every search and
  upload is logged (the `events` table), and the Super Admin panel shows all
  conversations plus RAG metrics — volume, latency, zero-result rate,
  MCP vs web split, top corpora. Seed the first super admin with
  `python3 scripts/set_super_admin.py you@example.com`.

- **Search/index:** OpenRAG is the single retrieval engine (the same
  stack the RAG POC evaluates). Uploads are ingested by OpenRAG under
  their doc-id-prefixed names; searches filter to the caller's corpus
  by filename. One user's documents are invisible to everyone else.
  Chat answers are grounded by the A2A agent (`charts/khoj-agent`)
  with an OpenAI model over the retrieved OpenRAG chunks (extractive
  fallback when no key is configured).
- **Auth:** built in. Email/password accounts live in SQLite; the app
  issues its own JWTs and verifies them on every API and MCP call.
  No Firebase, no external identity service.
- **MCP server:** `POST /mcp` (Streamable HTTP). Three tools:
  `search_documents`, `list_my_documents`, `list_my_corpora`. Users paste the config from
  the web UI ("Use Khoj in your agent") into Claude/Cursor/any MCP
  client; the Bearer token is their Khoj token (7-day lifetime —
  grab a fresh config from the UI when it expires).
- **App:** one FastAPI container serving UI + API + MCP, plus the A2A
  chat agent container. All state in one data dir: SQLite (accounts,
  orgs, uploads, logs) + the originals storage backend — local disk by
  default, or a GCS bucket if the deployer picks `KHOJ_STORAGE=gcs`.
  The only secret the stack needs is its token-signing secret.
- **Hosting:** no VM. Everything runs as containers on a GKE Autopilot
  cluster, one Helm chart per service under `charts/` — `khoj` and
  `khoj-agent` for this product, plus the OpenRAG stack it runs on.
  `cloudbuild.yaml` builds and deploys on push to `main` (manual
  submit until the GitHub trigger is connected).

The original uploaded files are also kept by the storage backend
(`users/{uid}/…`, `orgs/{orgId}/…`) as the source of truth for
re-ingestion into OpenRAG.

## Run (local smoke test)

```bash
cp .env.local.example .env.local   # or run scripts/start-local.sh --setup-only
docker compose up -d --build
# UI on http://<host>:8081
```

## Deploy (GKE)

```bash
helm install khoj charts/khoj   # per-service, same pattern for charts/openrag-*
```

## One-time pieces

1. Deploy the OpenRAG stack (`charts/openrag-*`) — it is the only
   index; Khoj reaches it in-cluster at `http://openrag-backend:8000`
   (or any reachable OpenRAG URL).
2. Create the token-signing secret: locally the start script generates
   one into `.env.local`; on GKE it lives in Google Secret Manager as
   `khoj-auth-secret` and syncs into both services via External
   Secrets Operator.
3. Only for `KHOJ_STORAGE=gcs`: create the corpus bucket and grant the
   Workload Identity Google service account
   `roles/storage.objectAdmin` on it.
4. The chat agent's OpenAI key (same key OpenRAG uses) lives in Google
   Secret Manager as `openrag-openai-api-key` and syncs into the
   cluster via External Secrets Operator.

## Cost shape

OpenRAG runs on the same GKE Autopilot cluster (OpenSearch + Langflow
+ Docling) — no per-query search pricing; the only metered AI cost is
the OpenAI usage OpenRAG and the chat agent already incur. Auth
and metadata are self-hosted (SQLite) at no per-user cost.
