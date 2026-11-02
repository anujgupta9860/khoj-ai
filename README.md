# Khoj.ai

Hosted document search with a per-user corpus: users sign up, upload
**their own documents**, and search only what they uploaded — on the web,
or from inside **their own agent** via Khoj's MCP server. Built for GCP
project `innovation-lab-2026`.

- **Search/index:** Vertex AI Search (Agent Search). Every document is
  imported with `structData.user_id` = the uploader's Firebase uid, and
  every search is filtered server-side by the uid in the verified token.
  One user's documents are invisible to everyone else.
- **Auth:** Firebase Authentication (email/password). The backend
  verifies the Firebase ID token on every API and MCP call.
- **MCP server:** `POST /mcp` (Streamable HTTP). Two tools:
  `search_documents`, `list_my_documents`. Users paste the config from
  the web UI ("Use Khoj in your agent") into Claude/Cursor/any MCP
  client; the Bearer token is their Firebase ID token (~1 h lifetime —
  grab a fresh config from the UI when it expires).
- **App:** one FastAPI container serving UI + API + MCP. GCP access via
  Workload Identity on GKE (ADC) — no keys in the repo. Credentials for
  backing services live in Google Secret Manager and sync into the
  cluster via External Secrets Operator (`deploy/external-secrets/`).
- **Hosting:** no VM. Everything runs as containers on a GKE Autopilot
  cluster, one Helm chart per service under `charts/` — `khoj` for this
  product, plus the OpenRAG stack as the back-office POC (not in the
  Khoj user path). `cloudbuild.yaml` builds and deploys on push to
  `main`; the Apigee proxy bundle lives in `apigee/`.

Migration note: documents imported before per-user metadata existed
(the 5 POC seed files, imported by hand in the console) carry no
`user_id`, so filtered searches cannot see them. Re-upload them through
the UI once and they join your corpus.

## Run (local smoke test)

```bash
cp .env.example .env   # fill in engine / data store / bucket / Firebase web config
docker compose up -d --build
# UI on http://<host>:8081
```

## Deploy (GKE)

```bash
helm install khoj charts/khoj   # per-service, same pattern for charts/openrag-*
```

## GCP pieces (one-time)

1. Enable the Discovery Engine API; create the corpus bucket.
2. Create a Vertex AI Search app + unstructured data store; note the
   engine + data store IDs for the chart values.
3. Add Firebase to the project; enable Email/Password sign-in; put the
   web config (`apiKey`, `authDomain`) in the chart values (public
   identifiers, not secrets).
4. Grant the Workload Identity Google service account:
   `roles/discoveryengine.viewer` (search), `roles/discoveryengine.editor`
   (imports), `roles/storage.objectAdmin` on the corpus bucket, and
   Firebase Admin access for token verification.

## Cost shape

Vertex AI Search is ~$0 at POC scale (first 10k queries/month free;
indexed data first 10 GB free), then $1.50 per 1,000 Standard queries
($4.00 Enterprise with generative answers). Firebase Auth email/password
and GKE Autopilot for one small service are effectively free at this
scale; Apigee evaluation covers the proxy.
