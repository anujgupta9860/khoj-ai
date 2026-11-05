# Khoj — Production Deployment on GKE (OpenRAG stack, External Secrets)

*Runbook · Nov 3, 2026 · Scope: the OpenRAG deployment and the Khoj services that consume it, on GKE Autopilot. Identity/authentication setup is intentionally out of scope for this guide.*

## 0. What you end up with

- GKE Autopilot cluster (regional) running, as Helm releases: `openrag-frontend`,
  `openrag-backend`, `langflow`, `opensearch`, `opensearch-dashboards`, `docling`,
  plus `khoj` (web/API) and `khoj-agent` (chat).
- All secrets sourced from Google Secret Manager via External Secrets Operator — none in
  Helm values, git, or container images.
- Public HTTP endpoints for Khoj, OpenRAG UI, Langflow, Dashboards (HTTPS step in §9).

## 1. Prerequisites

- GCP project with billing; enable APIs: `container`, `artifactregistry`,
  `secretmanager`, `cloudbuild`, `storage` (storage only for the optional gcs backend).
- Tools: `gcloud`, `kubectl`, `helm` (v3), `git`.
- An OpenAI API key (chat model `gpt-4o-mini`, embeddings `text-embedding-3-small`).
- A PersistentVolume for Khoj's data dir (SQLite + local originals). A GCS bucket is needed only with `KHOJ_STORAGE=gcs` —
  created once, outside Helm.

```bash
export PROJECT=innovation-lab-2026 REGION=us-central1 CLUSTER=khoj
gcloud config set project $PROJECT
gcloud services enable container.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com
```

## 2. Cluster + image registry

```bash
gcloud container clusters create-auto $CLUSTER --region $REGION
gcloud container clusters get-credentials $CLUSTER --region $REGION
gcloud artifacts repositories create khoj --repository-format=docker --location=$REGION
```

## 3. Secrets in Google Secret Manager (source of truth)

Create each secret **before** installing anything; External Secrets Operator only syncs
what exists.

| GSM secret | Used by | Notes |
|---|---|---|
| `openrag-openai-api-key` | openrag-backend, khoj-agent | Shared by both products; rotate once, both pick it up |
| `openrag-opensearch-admin-password` | opensearch, backend, dashboards | Set before first OpenSearch boot — it is baked into the cluster's admin user |
| `openrag-langflow-superuser-password` | langflow, backend | Also set the matching superuser **name** env (`LANGFLOW_SUPERUSER`) in values — the stock chart omits it and the backend cannot log in without it |
| `openrag-session-secret` | openrag-backend | Stable random value; changing it invalidates sessions |

```bash
printf '%s' "$OPENAI_API_KEY" | gcloud secrets create openrag-openai-api-key --data-file=-
# …repeat for the other three (use `gcloud secrets versions add` to rotate later)
```

## 4. External Secrets Operator

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace   # deployed version: 2.14
```

Grant the operator's service account `roles/secretmanager.secretAccessor` on the project
(Workload Identity), then apply the cluster-scoped store — note: with ESO 2.14 only the
`external-secrets.io/v1` API is served; v1beta1 manifests will fail.

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: { name: gcpsm }
spec:
  provider:
    gcpsm: { projectID: innovation-lab-2026 }   # auth via Workload Identity
```

Each chart then carries an `ExternalSecret` per credential, e.g.:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: { name: khoj-agent-openai }
spec:
  refreshInterval: 1h
  secretStoreRef: { name: gcpsm, kind: ClusterSecretStore }
  target: { name: khoj-agent-secrets, creationPolicy: Owner }
  data:
    - secretKey: OPENAI_API_KEY
      remoteRef: { key: openrag-openai-api-key }
```

Verify before deploying workloads:

```bash
kubectl get clustersecretstore gcpsm          # STATUS: Valid
kubectl get externalsecret -A                 # SECRETSTORE / SYNCED
kubectl get secret khoj-agent-secrets         # exists (never print its contents)
```

## 5. Deploy OpenRAG (Helm, one release per service)

Order matters: OpenSearch → backend state → Langflow → backend → frontend/dashboards.

Non-default values that production needs (all learned during bring-up):

1. **`enableServiceLinks: false` on every workload.** Kubernetes service-link env vars
   (`OPENRAG_BACKEND_PORT`, …) collide with the apps' own configuration — symptoms seen:
   a corrupted OIDC discovery URL baked at build time and search returning 401.
2. **Custom OpenSearch image** (`langflowai/openrag-opensearch`, OpenSearch 3.8) — the stock
   image lacks the **jvector** engine the `documents` index requires; pair it with
   Dashboards 3.0.
3. **PVCs for backend state** — mount `/app/keys`, `/app/config`, `/app/data` on a
   persistent volume. Upstream keeps workspace config (`openrag.db`) and keys on local
   disk; without PVCs a pod restart silently resets configuration.
4. **Backend env**: `OPENSEARCH_HOST=opensearch`, `OPENSEARCH_PORT=9200`; session secret
   from the ESO-synced secret (§3/§4), never a values entry.
5. **Frontend env**: `OPENRAG_BACKEND_HOST=openrag-backend`, `OPENRAG_BACKEND_PORT=8000`,
   `OPENRAG_BACKEND_SSL=false` (the frontend proxy is how `/api/*` reaches the backend).

Then the one-time application bootstrap, in the OpenRAG UI (or API):
- Complete onboarding with the OpenAI provider + key from GSM.
- **Wizard quirk:** if onboarding is done via API, also set `onboarding.current_step` to the
  final step in `/app/data/openrag.db` (table `workspace_config`) and restart the backend,
  or the UI keeps showing the wizard.
- Enable **OCR** in the knowledge/workspace config (`ocr = true`) if scanned PDFs matter —
  it ships off.
- Confirm provider health and that the sample documents ingest.

## 6. Deploy Khoj (Helm)

Key values (secrets all via ESO references, §4):

- `khoj` (web): `OPENRAG_URL=http://openrag-backend:8000` — the backend's **unprefixed**
  routes are the contract (`/search`, `/files`, `/router/upload_ingest`, `/knowledge-filter`);
  `/api/*` will 404 in-cluster. `KHOJ_MIN_SCORE` (relevance floor, default `1.5`); storage backend values (§5).
- `khoj-agent`: `OPENRAG_URL` same; `OPENAI_MODEL=gpt-4o-mini` (match OpenRAG's configured
  chat model); OpenAI key via its ExternalSecret (§4).
- Before first traffic, create the GSM secret `khoj-auth-secret` (any long random
  value) — both Khoj services sync it as their shared token-signing secret. Accounts
  and metadata live in SQLite on the data PVC; no database provisioning or indexes
  are needed.

## 7. Build & release pipeline (Cloud Build)

`cloudbuild.yaml` builds the Khoj images (and the patched OpenRAG backend image), pushes
them to Artifact Registry **inside the build steps** (top-level `images:` only pushes on
overall success), and runs `helm upgrade` with a **static-token kubeconfig** — the stock
helm builder image has no `gke-gcloud-auth-plugin`. The Cloud Build service account needs
Artifact Registry Writer + Kubernetes Engine Developer.

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=SHORT_SHA=$(git rev-parse --short HEAD)
kubectl rollout status deploy/khoj deploy/khoj-agent
```

Optional: create the GitHub trigger in the Cloud Build console (one OAuth authorization) to
deploy on push instead of manual submits.

## 8. Verification checklist (run after every deploy)

1. `kubectl get pods` — all releases Running; `kubectl get externalsecret -A` all SYNCED.
2. OpenRAG UI loads; provider health HEALTHY; Dashboards opens.
3. Upload a test document via Khoj → status reaches **ready**; the file appears in OpenRAG's
   file list *and* as a Knowledge-tab filter (Khoj registers one per upload).
4. Khoj search for a phrase from the document returns it; an unrelated nonsense query
   returns **zero** results (relevance floor working).
5. Khoj chat answers a content question with citations; ask a second test account about the
   first account's document — it must find nothing.
6. Backend route sanity from any pod:
   `curl -s http://openrag-backend:8000/files` returns JSON (not a 404 page).

## 9. Production hardening still required

- **Domain + HTTPS**: put the four public services behind a proper ingress with a managed
  certificate (GKE Gateway + Google-managed certs, or cert-manager). Today they are HTTP on
  load-balancer IPs — fine for a POC, not for real users.
- **Restrict operator surfaces**: Langflow and Dashboards are public in the POC; in
  production, put them on internal ingress or IP allow-list only. The OpenRAG UI is an
  operator tool — it searches the *whole* index; tenant isolation exists only in Khoj.
- **Backups**: snapshot the Khoj data PVC (it holds khoj.db + local originals), GCS bucket versioning (gcs backend only), OpenSearch snapshots to
  GCS. The OpenRAG backend PVC (workspace config) is small and critical — snapshot it too.
- **Secret rotation**: `gcloud secrets versions add` + `kubectl rollout restart` of the
  consuming deployment (ESO refreshInterval also propagates within an hour).
- **Cost watch**: GKE Autopilot bills per pod request; the idle stack is modest, but
  OpenSearch is the heavyweight — right-size its requests before scaling tenants.

## 10. Troubleshooting (symptoms seen in bring-up)

| Symptom | Cause | Fix |
|---|---|---|
| Upload fails: "Ingestion trigger: Not Found" | Caller used `/api/*` paths against the backend | Use unprefixed backend routes in-cluster (§6) |
| Search returns 401 from a fresh install | Bad OIDC/JWKS URL baked from injected service-link env | `enableServiceLinks: false` on the OpenSearch/backend workloads, restart |
| Index creation fails / jvector errors | Stock OpenSearch image lacks the engine | Use the custom OpenSearch 3.8 image (§5.2) |
| Backend "forgets" config after restart | Workspace config on ephemeral disk | PVCs on `/app/keys`, `/app/config`, `/app/data` (§5.3) |
| Onboarding wizard never completes (API-driven) | `onboarding.current_step` left at 0 | Set to final step in `openrag.db`, restart backend (§5) |
| Langflow login/429 loops, flows locked (423) | Stored flows are locked upstream | Edit by delete + re-import; expect JWT expiry ~45 min |
| Chat returns 401 while the UI works | Web/agent signing secrets differ | Ensure both charts sync the same `khoj-auth-secret` GSM key |
| Junk queries list the user's documents | kNN nearest-neighbour behaviour on small corpora | Relevance floor `KHOJ_MIN_SCORE` (§6); re-measure as corpora grow |
