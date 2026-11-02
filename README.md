# Khoj.ai

Hosted document search: users sign up, sign in, and search a **global
corpus** of documents. Only the admin can upload; uploads are indexed for
everyone. Built for GCP project `innovation-lab-2026`.

- **Search/index:** Vertex AI Search (Agent Search) — data store fed from a
  GCS bucket. Standard search + generative summary with citations.
- **Auth:** Firebase Authentication (email/password). The backend verifies
  the Firebase ID token on every API call; an `admin` custom claim gates
  uploads (see `scripts/set_admin.py`).
- **App:** one FastAPI container serving the UI + API. GCP access via
  Workload Identity on GKE (ADC) — no keys in the repo.
- **Hosting:** no VM. Everything runs as containers on a GKE Autopilot
  cluster in `innovation-lab-2026`, one Helm chart per service under
  `charts/` — `khoj` for this product, plus the OpenRAG stack
  (backend, frontend, langflow, opensearch, dashboards, docling) as the
  back-office POC. OpenRAG is not in the Khoj user path.

## Run (local smoke test)

```bash
cp .env.example .env   # fill in engine / data store / bucket / Firebase web config
docker compose up -d --build
# UI on http://<host>:8081
```

## Deploy (GKE)

```bash
helm install khoj charts/khoj -f values-prod.yaml   # per-service, same pattern for charts/openrag-*
```

## GCP pieces (one-time)

1. Enable the Discovery Engine API; create the corpus bucket.
2. Create a Vertex AI Search app + unstructured data store pointing at the
   bucket; note the engine + data store IDs for `.env`.
3. Add Firebase to the project; enable Email/Password sign-in; put the web
   config (`apiKey`, `authDomain`) in `.env`.
4. Grant the VM service account: `roles/discoveryengine.viewer` (search),
   `roles/discoveryengine.editor` (imports), `roles/storage.objectAdmin`
   on the corpus bucket, and Firebase Admin access for token verification.
5. Grant yourself admin: `python3 scripts/set_admin.py you@example.com`.

## Cost shape

VM hosting aside, Vertex AI Search is ~$0 at POC scale (first 10k
queries/month free; indexed data first 10 GB free), then $1.50 per 1,000
Standard queries ($4.00 Enterprise with generative answers).
