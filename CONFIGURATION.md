# Khoj — configuration guide for AI agents

**Audience: an AI agent (Claude or any other) asked to configure, run, or
deploy Khoj.** This file is the single source of truth for every config
surface in the project. Follow it exactly; where it says "ask the user",
stop and ask — never invent a secret value, and never commit one.

## 0. Rules for agents

1. **Secrets never go in git, Helm `values.yaml`, or chat output.** Secret
   *values* live only in Google Secret Manager (GKE deploys) or the local
   gitignored `.env.local` (local runs). Config files may reference secret
   *names* only.
2. This repo is **public**. Before committing, scan the diff for tokens,
   keys, and passwords. (The Firebase web `apiKey` in
   `charts/khoj/values.yaml` is a public client identifier, NOT a secret —
   leave it alone.)
3. When verifying wiring, print secret/env **names only**, never values
   (`env | grep -o '^JIRA_[A-Z_]*'`, `kubectl get secret …` existence only).
4. Config precedence: Helm values (GKE) → container env → app defaults.
   Locally: `.env.local` → shell env → app defaults.
5. One change at a time, then re-verify with the checklist in §7.

## 1. System in one paragraph

Khoj is a document-search product: a web app (FastAPI, `app/`) with a
Firebase sign-in UI, an A2A chat agent (`agent/`, JSON-RPC
`message/send` + streaming), and **OpenRAG** as the only retrieval/
indexing engine (six Helm charts in `charts/openrag-*`). State: uploads
in a GCS bucket, metadata/memberships/logs in Firestore, document chunks
in OpenRAG's OpenSearch. Live deployment: GKE Autopilot cluster `khoj`,
project `innovation-lab-2026`, namespace `default`, site
http://34.8.195.76, OpenRAG UI http://34.102.202.103.

## 2. Configuration surfaces

| Surface | Where | Holds |
|---|---|---|
| Local env file | `.env.local` (gitignored), written by `scripts/start-local.sh` | Local runs: every env var below |
| Web chart | `charts/khoj/values.yaml` | Project, bucket, OpenRAG/agent URLs, Firebase web config, **Jira block** |
| Agent chart | `charts/khoj-agent/values.yaml` | Project, bucket, OpenRAG URL, model, OpenAI secret reference |
| OpenRAG charts | `charts/openrag-*/values.yaml` | Images, storage, service names |
| GSM secrets | project `innovation-lab-2026` | All secret values; synced by External Secrets Operator (ClusterSecretStore `gcpsm`) into k8s Secrets |
| Firestore | collections `orgs`, `users`, `file_uploads`, `search_logs` | Runtime **data**, not deploy config — never hand-edit except the documented super-admin bootstrap (`scripts/set_super_admin.py`) |

## 3. Environment variable reference

### Web app (`app/main.py`)

| Var | Required | Default | Set from (Helm) | Notes |
|---|---|---|---|---|
| `GCP_PROJECT` | yes | — | `gcp.project` | Firestore + Firebase project |
| `KHOJ_BUCKET` | yes | — | `gcp.bucket` | GCS originals bucket |
| `OPENRAG_URL` | no | `http://openrag-backend:8000` | `openrag.url` | **Unprefixed** backend routes (`/search`, `/files`, …); see ADR 010 |
| `KHOJ_AGENT_URL` | no | `http://khoj-agent` | `agent.url` | A2A agent base URL (no port = 80 via its Service) |
| `FIREBASE_API_KEY` | no | built-in fallback | `firebase.apiKey` | Public web identifier |
| `FIREBASE_AUTH_DOMAIN` | no | `<project>.firebaseapp.com` | `firebase.authDomain` | Public |
| `KHOJ_MIN_SCORE` | no | `1.5` | — (not in values yet) | Relevance floor: search chunks below this score are dropped (ADR 007). To change in GKE, add an env entry in `charts/khoj/templates/deployment.yaml` |
| `JIRA_BASE_URL` | Jira only | unset = off | `jira.baseUrl` | e.g. `https://khoj.atlassian.net` |
| `JIRA_EMAIL` | Jira only | unset | `jira.email` | Atlassian account email for Basic auth |
| `JIRA_PROJECT_KEY` | Jira only | unset | `jira.projectKey` | e.g. `RAG` — must exist on the site |
| `JIRA_ISSUE_TYPE` | no | `Task` | `jira.issueType` | Must exist in the project's issue-type scheme |
| `JIRA_API_TOKEN` | Jira only | unset | secretKeyRef ← k8s Secret `jira.secretName` (default `khoj-jira`) ← GSM `jira.externalSecret.gsmKey` (default `khoj-jira-api-token`) | **Value never in values.yaml**; the pod reference is `optional: true` so the app runs with Jira off until the secret exists |

Feature check at runtime: `GET /api/jira/status` (authenticated) returns
`{enabled, projectKey, baseUrl}`. Ticket creation: `POST /api/jira/ticket`
with `{question, answer, orgId?}`.

### Chat agent (`agent/main.py`)

| Var | Required | Default | Set from (Helm) | Notes |
|---|---|---|---|---|
| `GCP_PROJECT` | no | `innovation-lab-2026` | values | |
| `KHOJ_BUCKET` | no | `khoj-ai-corpus-innovation-lab` | values | Read-only use (full-text handoff) |
| `OPENRAG_URL` | no | `http://openrag-backend:8000` | values | Same unprefixed routes |
| `OPENAI_API_KEY` | no | unset | secret `khoj-agent-openai` ← GSM `openrag-openai-api-key` | Unset → agent answers extractively (no LLM) |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | values | Keep equal to OpenRAG's configured agent model for output parity |
| `PORT` | no | `8080` | chart | Listen port |

Both services call `firebase_admin.initialize_app()` with Application
Default Credentials — in GKE that is Workload Identity (GSA
`khoj-app@innovation-lab-2026.iam.gserviceaccount.com`, bound to service
accounts `default/khoj` and `default/khoj-agent`); locally it is the
user's `gcloud` ADC or `GOOGLE_APPLICATION_CREDENTIALS`.

## 4. Secrets inventory (Google Secret Manager → cluster)

| GSM secret | Consumed by | k8s Secret | Rotate by |
|---|---|---|---|
| `openrag-openai-api-key` | OpenRAG backend **and** Khoj agent | `openrag-secrets`, `khoj-agent-openai` | add new GSM version; ESO syncs ≤1h (or annotate the ExternalSecret to refresh now) |
| `openrag-opensearch-admin-password` | OpenSearch + backend | `opensearch-secrets`, `openrag-secrets` | new version + restart OpenSearch StatefulSet and backend |
| `openrag-langflow-superuser-password` | Backend (Langflow login) | `openrag-secrets` | new version + restart backend |
| `openrag-session-secret` | Backend sessions | `openrag-secrets` | new version + restart backend |
| `khoj-jira-api-token` | Khoj web (`JIRA_API_TOKEN`) | `khoj-jira` | new version; rollout restart `deploy/khoj` if the pod predates the secret |

## 5. Procedure A — run everything locally

```bash
bash scripts/start-local.sh        # asks all config, writes .env.local,
                                   # builds .venv-local, starts web+agent
bash scripts/start-local.sh --setup-only   # config only
bash scripts/start-local.sh --yes          # reuse .env.local, no prompts
```

Manual equivalent (only if the script cannot be used): export the §3
variables, `pip install -r requirements.txt -r agent/requirements.txt`,
then `uvicorn app.main:app --port 8080` and
`PYTHONPATH=. uvicorn --app-dir agent main:app --port 8090`, with
`KHOJ_AGENT_URL=http://localhost:8090`. OpenRAG is external — point
`OPENRAG_URL` at a local backend (`http://localhost:8000`) or the GKE
stack through its app proxy (`http://34.102.202.103/api`). Details:
`docs/local-run.md`.

## 6. Procedure B — deploy to GKE

Preferred: `gcloud builds submit --config cloudbuild.yaml
--substitutions=SHORT_SHA=$(git rev-parse --short HEAD)` — builds both
images and helm-upgrades releases `khoj` and `khoj-agent` (waits 5m).

OpenRAG charts are upgraded manually, in this order, only when their
values/images change:
`openrag-opensearch`, `openrag-docling`, `openrag-langflow`,
`openrag-backend`, `openrag-frontend`, `openrag-opensearch-dashboards`
(release name = chart name, namespace `default`).

Never run the Cloud Build trigger path — the GitHub trigger is not
connected; builds are submitted manually as above.

## 7. Procedure C — configure or change Jira (worked example of §3)

1. Ask the user to create an API token at id.atlassian.com
   ("API tokens") for the account that owns the project. **They paste it
   once; store it immediately (step 2) and never write it anywhere else.**
2. Store it: `gcloud secrets create khoj-jira-api-token` (or
   `versions add`) in project `innovation-lab-2026`.
3. Set non-secret values in `charts/khoj/values.yaml` → `jira:`:
   `baseUrl`, `email`, `projectKey`, `issueType` (verify the project key
   first: `GET {base}/rest/api/3/project` with the token). Commit + push.
4. Deploy (Procedure B). The chart's ExternalSecret
   (`charts/khoj/templates/externalsecret.yaml`) syncs GSM → `khoj-jira`.
5. Verify (§8): ExternalSecret `SecretSynced`, secret exists, pod env
   **names** present, then one real ticket from the chat UI (🎫 button
   under an answer) lands in the Jira project.

## 8. Verification checklist

```bash
curl -s http://34.8.195.76/api/config                 # Firebase config JSON
curl -s http://34.102.202.103/api/users/me            # roles incl. admin
curl -s http://<agent>/.well-known/agent-card.json    # agent card
kubectl rollout status deployment/khoj deployment/khoj-agent
kubectl get externalsecret            # all SecretSynced / Ready=True
kubectl exec deploy/khoj -- sh -c 'env | grep -o "^JIRA_[A-Z_]*" | sort'
# End-to-end: sign in, upload a small file, search it, ask in chat,
# confirm the 🎫 button creates a ticket in the configured project.
```

## 9. Troubleshooting map

| Symptom | Likely cause | Fix |
|---|---|---|
| Chat nav/button missing in OpenRAG UI | browser session failed the `/api/users/me` fetch (permissions resolve empty) | open the site in a real browser (not an in-app sheet), hard refresh |
| Search returns junk/empty for unrelated queries | relevance floor | `KHOJ_MIN_SCORE` (§3) — genuine hits score ≥ ~1.65, junk ~1.25–1.29 |
| 🎫 button never appears | `/api/jira/status` says disabled | a `JIRA_*` value empty, or the `khoj-jira` secret not synced yet (§7 step 5) |
| Ticket creation 502 | Jira rejected the request (bad project key/issue type/token) | re-check §3 Jira row; verify with `GET /rest/api/3/myself` |
| Pods pending after values change | ESO/GSM name mismatch | ExternalSecret status shows the failing remote key |
| Sign-in fails locally | no Google credentials | `gcloud auth application-default login` or set the key path in `.env.local` |

## 10. Decisions behind this configuration

`docs/sdr-adr.md` (ADRs 001–012) and `docs/openrag-api-usage.md` explain
why: single OpenRAG engine, unprefixed routes, filename-convention
isolation, relevance floor, secrets via ESO ← GSM. Read them before
proposing structural changes.
