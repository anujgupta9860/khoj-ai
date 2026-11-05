# Run Khoj locally (end to end)

## Option A — the complete stack, Docker only

`docker-compose.yml` runs **everything**: Khoj web + A2A agent plus the
full OpenRAG stack (OpenSearch, Dashboards, Docling, Langflow, backend,
frontend) — the same images and wiring as the GKE deployment, with
local volumes and a plain `.env` file instead of GKE/GSM/ESO.

```bash
cp .env.local.example .env   # set OPENAI_API_KEY + KHOJ_AUTH_SECRET
docker compose up --build
```

| Service | URL | Sign-in |
|---|---|---|
| Khoj | http://localhost:8081 | your own account (super-admin email from .env) |
| OpenRAG UI | http://localhost:3000 | no-auth mode |
| Langflow | http://localhost:7860 | `langflow` / `$LANGFLOW_SUPERUSER_PASSWORD` |
| Dashboards | http://localhost:5601 | `admin` / `$OPENSEARCH_PASSWORD` |
| Docling | http://localhost:5001 | — |
| OpenSearch API | http://localhost:9200 | `admin` / `$OPENSEARCH_PASSWORD` |

First boot takes a while (OpenSearch + Langflow start slowly, and
ingestion flows load). A one-shot `openrag-init` service completes
OpenRAG onboarding server-side — provider + models from `.env`, OCR
on, chunks 1000/200 — so there is no setup wizard. Watch it with
`docker compose logs -f openrag-init`; re-run it after changing models
with `docker compose run --rm openrag-init`.

Fully local models (no OpenAI): set `LLM_PROVIDER=ollama`,
`EMBEDDING_PROVIDER=ollama`, the model names, and keep
`OLLAMA_ENDPOINT=http://host.docker.internal:11434` with Ollama
running on the host.

Khoj-only mode (OpenRAG hosted elsewhere, e.g. GKE): set `OPENRAG_URL`
in `.env` and start just the product services:
`docker compose up --no-deps khoj khoj-agent`.

Stop with `docker compose down` (add `-v` to wipe all data volumes).

## Option B — no Docker (Python processes)

One script asks for every config value and starts the product on your
machine — web app + A2A chat agent — pointed at an OpenRAG you choose.

```bash
bash scripts/start-local.sh
```

- Answers are saved to `.env.local` (gitignored; contains the secrets
  you typed, so keep the file private). Re-runs offer to reuse it.
  `--setup-only` writes the config without starting anything;
  `--yes` reuses it without asking.
- Dependencies go into `.venv-local`; logs land in `.local-run/`
  (`web.log`, `agent.log`). Ctrl+C stops both services.
- You get: Web UI on the chosen port (default 8080) and the agent's
  AgentCard on its port (default 8090).

### What you need (Option B)

- Python 3.10+.
- No accounts or credentials to prepare: auth is built in and all
  data lives in the data dir you choose. Only if you pick
  `KHOJ_STORAGE=gcs` do you need Google credentials (gcloud ADC or a
  service-account key) for the originals bucket.
- A running OpenRAG to point `OPENRAG_URL` at — the Option A stack's
  backend (`http://localhost:8000`), another local backend (e.g. the
  openrag-gemma-local stack), or the deployed GKE stack through its
  app proxy (`http://34.102.202.103/api`; Khoj appends `/search`,
  `/files`, … and the proxy strips the `/api` prefix).
- Optional: an OpenAI key for the chat agent (blank = extractive
  answers), and Jira settings + API token for the 🎫 ticket button
  (blank token = feature off). Every Atlassian value the script asks
  for mirrors the Helm values of the same name.

## GKE

The same stack deploys to GKE from the Helm charts under `charts/` —
see `docs/production-deploy-gke.md`. Compose is the local twin of that
deployment: same services, same hostnames (`openrag-backend:8000`,
`khoj`, `khoj-agent`, `docling:5001`, `opensearch:9200`).
