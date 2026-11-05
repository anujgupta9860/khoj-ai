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
   keys, and passwords.
3. When verifying wiring, print secret/env **names only**, never values
   (`env | grep -o '^JIRA_[A-Z_]*'`, `kubectl get secret …` existence only).
4. Config precedence: Helm values (GKE) → container env → app defaults.
   Locally: `.env.local` → shell env → app defaults.
5. One change at a time, then re-verify with the checklist in §8.

## 1. System in one paragraph

Khoj is a document-search product: a web app (FastAPI, `app/`) with a
built-in email/password sign-in, an A2A chat agent (`agent/`, JSON-RPC
`message/send` + streaming), and **OpenRAG** as the only retrieval/
indexing engine (six Helm charts in `charts/openrag-*`). **No Firebase
and no required cloud services** (ADR-013): accounts, orgs, memberships,
invites, upload records and the activity log live in one SQLite file;
uploaded originals live in a storage backend the deployer chooses —
local disk (default) or a GCS bucket. The agent holds no data
credentials: it verifies the caller's token locally and asks the web
service for the authorized corpus and document text. Live deployment:
GKE Autopilot cluster `khoj`, project `innovation-lab-2026`, namespace
`default`, site http://34.8.195.76, OpenRAG UI http://34.102.202.103 —
GKE is now just the current hosting choice.

## 2. Configuration surfaces

| Surface | Where | Holds |
|---|---|---|
| Local env file | `.env.local` (gitignored), written by `scripts/start-local.sh` | Local runs: every env var below |
| Web chart | `charts/khoj/values.yaml` | Data dir + PVC, **storage backend choice**, auth (super-admin email, secret reference), OpenRAG/agent URLs, **Jira block** |
| Agent chart | `charts/khoj-agent/values.yaml` | Web service URL, OpenRAG URL, model, OpenAI + auth secret references |
| OpenRAG charts | `charts/openrag-*/values.yaml` | Images, storage, service names |
| Data dir | `KHOJ_DATA_DIR` (PVC on GKE, folder locally) | `khoj.db` (SQLite: everything), `files/` (originals, local backend), `auth_secret` (only when `KHOJ_AUTH_SECRET` is unset) |
| GSM secrets | project `innovation-lab-2026` | Secret values for the GKE deployment; synced by External Secrets Operator (ClusterSecretStore `gcpsm`) into k8s Secrets |

## 3. Environment variable reference

### Shared

| Var | Required | Default | Notes |
|---|---|---|---|
| `KHOJ_DATA_DIR` | no | `./data` (cwd-relative) | Web only (the agent is stateless). Holds `khoj.db` + `files/` |
| `KHOJ_AUTH_SECRET` | recommended | auto-generated into `<data dir>/auth_secret` | Signs session tokens. **Web and agent MUST share the same value** — in GKE both sync the same GSM key; locally the start script generates one into `.env.local`. Without a shared secret, chat calls fail 401 |
| `KHOJ_TOKEN_TTL_HOURS` | no | `168` (7 days) | Session token lifetime |
| `OPENRAG_URL` | no | `http://openrag-backend:8000` | **Unprefixed** backend routes (`/search`, `/files`, …); see ADR 010 |

### Web app (`app/main.py`)

| Var | Required | Default | Set from (Helm) | Notes |
|---|---|---|---|---|
| `KHOJ_STORAGE` | no | `local` | `storage.backend` | `local` = originals on disk under `<data dir>/files`; `gcs` = originals in `KHOJ_BUCKET`. Same object keys either way (`users/<uid>/…`, `orgs/<orgId>/…`) |
| `KHOJ_BUCKET` | gcs only | — | `storage.bucket` | Only read when `KHOJ_STORAGE=gcs`; that mode also needs Google credentials (Workload Identity on GKE) |
| `KHOJ_SUPER_ADMIN_EMAIL` | no | unset | `auth.superAdminEmail` | Comma-separated. Accounts with these emails become platform super admins at register/login (seeded as platform-org admins) |
| `KHOJ_AGENT_URL` | no | `http://khoj-agent` | `agent.url` | A2A agent base URL |
| `KHOJ_MIN_SCORE` | no | `1.5` | — (not in values yet) | Relevance floor: search chunks below this score are dropped (ADR 007). To change in GKE, add an env entry in `charts/khoj/templates/deployment.yaml` |
| `JIRA_BASE_URL` | Jira only | unset = off | `jira.baseUrl` | e.g. `https://khoj.atlassian.net` |
| `JIRA_EMAIL` | Jira only | unset | `jira.email` | Atlassian account email for Basic auth |
| `JIRA_PROJECT_KEY` | Jira only | unset | `jira.projectKey` | e.g. `RAG` — must exist on the site |
| `JIRA_ISSUE_TYPE` | no | `Task` | `jira.issueType` | Must exist in the project's issue-type scheme |
| `JIRA_API_TOKEN` | Jira only | unset | secretKeyRef ← k8s Secret `jira.secretName` (default `khoj-jira`) ← GSM `jira.externalSecret.gsmKey` (default `khoj-jira-api-token`) | **Value never in values.yaml**; the pod reference is `optional: true` so the app runs with Jira off until the secret exists |

Auth endpoints: `POST /api/auth/register` and `POST /api/auth/login`
(JSON `{email, password}` → `{token, email, uid}`; passwords ≥ 8 chars,
stored PBKDF2-hashed). Feature check: `GET /api/jira/status`
(authenticated) returns `{enabled, projectKey, baseUrl}`. Ticket
creation: `POST /api/jira/ticket` with `{question, answer, orgId?}`.

### Chat agent (`agent/main.py`)

| Var | Required | Default | Set from (Helm) | Notes |
|---|---|---|---|---|
| `KHOJ_WEB_URL` | no | `http://khoj` | `web.url` | The web service. The agent fetches the caller's corpus (`GET /api/agent/corpus`) and document originals (`GET /api/documents/{id}/raw`) from it, forwarding the caller's token — authorization is enforced by the web service on every call |
| `OPENAI_API_KEY` | no | unset | secret `khoj-agent-openai` ← GSM `openrag-openai-api-key` | Unset → agent answers extractively (no LLM) |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | values | Keep equal to OpenRAG's configured agent model for output parity |
| `PORT` | no | `8080` | chart | Listen port |

## 4. Secrets inventory (Google Secret Manager → cluster)

| GSM secret | Consumed by | k8s Secret | Rotate by |
|---|---|---|---|
| `khoj-auth-secret` | Khoj web **and** agent (`KHOJ_AUTH_SECRET`) | `khoj-auth`, `khoj-agent-auth` | add new GSM version; ESO syncs ≤1h; rollout restart both deployments. All existing sessions invalidate |
| `openrag-openai-api-key` | OpenRAG backend **and** Khoj agent | `openrag-secrets`, `khoj-agent-openai` | add new GSM version; ESO syncs ≤1h (or annotate the ExternalSecret to refresh now) |
| `khoj-jira-api-token` | Khoj web (`JIRA_API_TOKEN`) | `khoj-jira` | new version; rollout restart `deploy/khoj` if the pod predates the secret |
| `openrag-opensearch-admin-password` | OpenSearch + backend | `opensearch-secrets`, `openrag-secrets` | new version + restart OpenSearch StatefulSet and backend |
| `openrag-langflow-superuser-password` | Backend (Langflow login) | `openrag-secrets` | new version + restart backend |
| `openrag-session-secret` | Backend sessions | `openrag-secrets` | new version + restart backend |

## 5. Procedure A — run everything locally

```bash
# Complete stack, Docker only (Khoj + agent + the full OpenRAG stack;
# the openrag-init service finishes OpenRAG onboarding automatically):
cp .env.local.example .env         # set OPENAI_API_KEY + KHOJ_AUTH_SECRET
docker compose up --build          # Khoj :8081 · OpenRAG :3000 · Langflow :7860

# No-Docker alternative (web+agent as Python processes, OpenRAG external):
bash scripts/start-local.sh        # asks all config, writes .env.local,
                                   # builds .venv-local, starts web+agent
bash scripts/start-local.sh --setup-only   # config only
bash scripts/start-local.sh --yes          # reuse .env.local, no prompts
```

The script asks for: ports, data dir, **storage backend (local|gcs)**,
super-admin email, signing secret (blank = generate), OpenRAG URL,
OpenAI key/model, and the Jira block. First run: register on the
sign-in page — the configured super-admin email becomes platform admin
automatically. Manual equivalent and details: `docs/local-run.md`.

## 6. Procedure B — deploy to GKE

Before the FIRST deploy of the post-ADR-013 version: create the GSM
secret `khoj-auth-secret` (any long random value) in project
`innovation-lab-2026`. Note the data reset: accounts/documents start
fresh on the new PVC (Firestore data does not migrate); users
re-register and re-upload.

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=SHORT_SHA=$(git rev-parse --short HEAD)
```
— builds both images and helm-upgrades releases `khoj` and
`khoj-agent` (waits 5m). The web chart creates the `khoj-data` PVC;
with default values the storage backend is `local` and no Workload
Identity binding is needed.

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
curl -s http://34.8.195.76/api/config                 # {"auth":"khoj","storage":"local"}
curl -s http://34.102.202.103/api/users/me            # roles incl. admin
curl -s http://<agent>/.well-known/agent-card.json    # agent card
kubectl rollout status deployment/khoj deployment/khoj-agent
kubectl get externalsecret            # all SecretSynced / Ready=True
kubectl get pvc khoj-data             # Bound
kubectl exec deploy/khoj -- sh -c 'env | grep -o "^JIRA_[A-Z_]*" | sort'
# End-to-end: register, upload a small file, search it, ask in chat,
# confirm the 🎫 button creates a ticket in the configured project.
# Local equivalents: python tests/test_app_local.py,
# python agent/test_agent_local.py, python tests/test_sqlite_stack.py
```

## 9. Troubleshooting map

| Symptom | Likely cause | Fix |
|---|---|---|
| Chat fails 401 while search works | web and agent have different `KHOJ_AUTH_SECRET` | sync the same GSM key into both charts (§4); locally, one `.env.local` drives both |
| Sign-in fails locally | stale token after the secret changed | sign out/in again; tokens invalidate when the secret rotates |
| Chat nav/button missing in OpenRAG UI | browser session failed the `/api/users/me` fetch (permissions resolve empty) | open the site in a real browser (not an in-app sheet), hard refresh |
| Search returns junk/empty for unrelated queries | relevance floor | `KHOJ_MIN_SCORE` (§3) — genuine hits score ≥ ~1.65, junk ~1.25–1.29 |
| 🎫 button never appears | `/api/jira/status` says disabled | a `JIRA_*` value empty, or the `khoj-jira` secret not synced yet (§7 step 5) |
| Ticket creation 502 | Jira rejected the request (bad project key/issue type/token) | re-check §3 Jira row; verify with `GET /rest/api/3/myself` |
| Uploads fail with storage errors (gcs mode) | `KHOJ_STORAGE=gcs` without bucket/credentials | set `storage.bucket` and the Workload Identity GSA, or switch to `local` |
| Pods pending after values change | ESO/GSM name mismatch, or PVC unbound | ExternalSecret status shows the failing remote key; `kubectl describe pvc khoj-data` |

## 10. Decisions behind this configuration

`docs/sdr-adr.md` (ADRs 001–013) and `docs/openrag-api-usage.md` explain
why: single OpenRAG engine, unprefixed routes, filename-convention
isolation, relevance floor, secrets via ESO ← GSM, and ADR-013 (no
Firebase; built-in auth; SQLite; pluggable originals storage). Read
them before proposing structural changes.
