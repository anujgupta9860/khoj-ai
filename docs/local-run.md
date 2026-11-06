# Run Khoj locally (end to end)

## Fastest path — one script, bring your own config files

Verified end to end on a Mac mini (Apple Silicon), 2026-11-05.

1. Clone the repo:

   ```bash
   git clone https://github.com/anujgupta9860/khoj-ai.git ~/khoj-ai
   cd ~/khoj-ai
   ```

2. Prepare the two config files and download them into `~/Downloads`.
   They hold the real secrets (OpenAI key, Jira token, passwords), so
   keep them in a private store of your own (a private cloud bucket, a
   password manager, …) and never commit them — both names are
   gitignored:
   - `env.docker` — for the Docker stack (becomes `.env`)
   - `env.local` — for the no-Docker script (becomes `.env.local`)

   Start from `.env.local.example` for the variable list. If your
   store is a GCS bucket, for example:

   ```bash
   gsutil cp gs://YOUR-PRIVATE-BUCKET/env.docker ~/Downloads/env.docker
   gsutil cp gs://YOUR-PRIVATE-BUCKET/env.local  ~/Downloads/env.local
   ```

3. Run one script — it installs/starts Docker if needed, places the
   config files, brings the whole stack up, waits until every piece
   answers, and prints the URLs:

   ```bash
   bash scripts/run-local-stack.sh
   ```

   Khoj lands on http://localhost:8081 (register there; the
   super-admin email from `.env` becomes platform admin). Stop with
   `bash scripts/run-local-stack.sh down`; follow logs with
   `bash scripts/run-local-stack.sh logs`.

Everything below is the manual equivalent of those steps.

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

Custom OpenAI-compatible endpoint (a proxy, gateway, or another
provider): set `OPENAI_BASE_URL` in `.env` — blank keeps the default
`https://api.openai.com/v1`. The Khoj agent uses it for its chat
calls, and openrag-init passes it to OpenRAG as the OpenAI provider's
`api_base`, so it covers OpenRAG's chat and embeddings too. On an
already-onboarded stack, re-apply it with
`docker compose run --rm openrag-init`. If the endpoint is internal
and resolves to a private IP, Langflow's SSRF guard blocks it —
allow the hostname in `.env` via
`LANGFLOW_SSRF_ALLOWED_HOSTS=<hostname>` and restart the stack. (The no-Docker script asks for
the same value; on GKE the agent takes it from the chart's
`openai.baseUrl` value, and OpenRAG's is set through its provider
settings.)

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

### Point the script run at the Option A OpenRAG

With the Docker stack up, its backend answers on
`http://localhost:8000` — repoint an existing `.env.local` and restart:

```bash
sed -i '' 's|^OPENRAG_URL=.*|OPENRAG_URL=http://localhost:8000|' .env.local
bash scripts/start-local.sh --yes
```

(macOS `sed` syntax — on Linux, drop the `''` after `-i`.)

(The Option A Khoj on :8081 already uses the local backend — this is
only for the Option B processes.) Note the local index starts empty:
documents ingested into a different OpenRAG (e.g. GKE) must be
re-uploaded before search/chat can find them here.

### What you need (Option B)

- Python 3.10+. ⚠️ On macOS the system `python3` is 3.9 and the agent
  fails at import (`TypeError: unsupported operand type(s) for |`).
  Install a current Python and make Homebrew win PATH:
  `brew install python@3.12`, then
  `echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile`
  and open a new terminal. `python3 --version` must print 3.10+
  before running the script; if a `.venv-local` was created by 3.9,
  delete it (`rm -rf .venv-local`) so it is rebuilt.
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

## Troubleshooting: frontend proxy SSL errors (ERR_SSL_WRONG_VERSION_NUMBER)

Symptom: `docker compose logs openrag-frontend` fills with
`Proxy error: TypeError: fetch failed ... ERR_SSL_WRONG_VERSION_NUMBER`.
The frontend is making a TLS call where plain HTTP is expected. Two
suspects: the container's `OPENRAG_BACKEND_SSL` is not `false`, or a
proxy injected by Docker Desktop / a corporate network is hijacking
service-to-service calls.

Inspect the RUNNING container's actual environment (the compose file
on disk is not proof of what the container got):

```
docker inspect khoj-openrag-frontend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -iE 'BACKEND|PROXY'
```

- `OPENRAG_BACKEND_SSL=true` in the output -> the container was created
  from a different compose state. Fix the value in `docker-compose.yml`
  (`OPENRAG_BACKEND_SSL: "false"`) and recreate:
  `docker compose up -d --force-recreate openrag-frontend`.
- Any `HTTP_PROXY` / `HTTPS_PROXY` in the output -> the proxy is the
  problem. The compose file sets `NO_PROXY` for all internal service
  names on every service (see `x-internal-env` in docker-compose.yml);
  make sure your checkout is current (`git pull`) and recreate the
  stack so the containers pick it up.

If the environment looks right, bisect the hop itself — this bypasses
the frontend app and tries the same plain-HTTP call from inside the
frontend container:

```
docker exec khoj-openrag-frontend node -e "fetch('http://openrag-backend:8000/files').then(r=>r.text()).then(t=>console.log('OK',t.slice(0,100))).catch(e=>console.log('ERR',e.cause&&e.cause.code))"
```

- `OK` -> the container network hop is healthy; the TLS attempt is
  inside the frontend's own code — inspect the bundled route
  (`.next/server/app/api/[...path]/route.js`) for the URL it builds.
- `ERR` with the SSL code -> the plain-HTTP request is intercepted
  between the containers; suspect the host's network stack (VPN /
  TLS-inspecting proxy), not the app.
- `ERR` with `ECONNREFUSED` -> the name resolved but nothing listens
  on the backend port: the backend container is down or restarting.
  Check `docker compose ps`, then `docker compose logs openrag-backend`.

If a bare request still fails while the manual fetch works, check
whether the frontend's bundled code reads the variable at runtime at
all — the app executes from the entrypoint's copy in
`/tmp/openrag-frontend`:

```
docker exec khoj-openrag-frontend sh -c 'grep -c OPENRAG_BACKEND_SSL "/tmp/openrag-frontend/.next/server/app/api/[...path]/route.js"'
```

- `1` or more -> the bundle looks the variable up at runtime; the
  running process's environment differs from the container config.
- `0` -> the value was compiled into the published image at build
  time and the compose setting cannot reach it; build the frontend
  from source instead of pulling the published image.
