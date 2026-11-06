#!/usr/bin/env bash
# Khoj — run the COMPLETE stack locally with one command: Khoj web +
# A2A agent plus the full OpenRAG stack (OpenSearch, Dashboards,
# Docling, Langflow, backend, frontend), via docker-compose.yml.
#
#   bash scripts/run-local-stack.sh          # install/start Docker if
#                                            # needed, place config,
#                                            # compose up, wait, print URLs
#   bash scripts/run-local-stack.sh down     # stop the stack (data kept)
#   bash scripts/run-local-stack.sh logs     # follow all logs
#
# Configuration comes from two env files YOU supply — they hold the
# real secrets, so keep them in a private store of your own and never
# commit them:
#   * env.docker -> .env        (required: docker compose reads .env)
#   * env.local  -> .env.local  (optional: for scripts/start-local.sh)
# The script finds each file in ~/Downloads, fetches it with gsutil
# when you set KHOJ_SETUP_BUCKET to your own private bucket, or takes
# an explicit path from KHOJ_ENV_DOCKER / KHOJ_ENV_LOCAL.
#
# Verified end to end on a Mac mini (Apple Silicon), 2026-11-05.
set -u
cd "$(dirname "$0")/.."
ROOT="$PWD"
BUCKET="${KHOJ_SETUP_BUCKET:-}"   # optional: your own private bucket

# Optional corporate-CA override: when corp-ca.pem sits in the repo
# root, the Khoj containers verify outbound HTTPS against that bundle
# too (TLS-inspecting corporate networks — e.g. Jira failing with
# CERTIFICATE_VERIFY_FAILED). See docker-compose.ca.yml.
COMPOSE=(docker compose)
if [ -f "$ROOT/corp-ca.pem" ]; then
  COMPOSE+=(-f docker-compose.ca.yml)
  echo "Found corp-ca.pem — Khoj containers will trust that CA bundle."
fi

cmd="${1:-up}"
case "$cmd" in
  down) "${COMPOSE[@]}" down; exit 0 ;;
  logs) "${COMPOSE[@]}" logs -f; exit 0 ;;
  up) ;;
  *) echo "unknown command: $cmd (use: up | down | logs)" >&2; exit 2 ;;
esac

# --- 1. Docker: CLI present? daemon running? -------------------------------
if ! command -v docker >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Docker not found — installing Docker Desktop via Homebrew…"
    brew install --cask docker || { echo "brew install failed" >&2; exit 1; }
  else
    echo "Docker is not installed. Install Docker Desktop or Colima first:" >&2
    echo "  https://docs.docker.com/get-docker/" >&2
    exit 1
  fi
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running — starting it…"
  if [ "$(uname)" = "Darwin" ]; then
    open -a Docker 2>/dev/null || true
  fi
  if command -v colima >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    # Colima route (used before Docker Desktop): give the stack room.
    colima start --cpu 4 --memory 12 || true
  fi
  for i in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 5
  done
  docker info >/dev/null 2>&1 || {
    echo "Docker daemon still not up after ~5 minutes." >&2
    echo "If Docker Desktop just launched, click through its first-run" >&2
    echo "dialogs, then re-run this script." >&2
    exit 1
  }
fi
echo "Docker is up."

# --- 2. Config: place .env (and .env.local) from the files you supply -----
place_env() { # place_env <name> <dest> <required 0|1> <explicit path>
  local src="$1" dest="$2" required="$3" explicit="${4:-}"
  if [ -f "$ROOT/$dest" ]; then
    echo "Found $dest — reusing it."
    return 0
  fi
  if [ -n "$explicit" ] && [ -f "$explicit" ]; then
    cp "$explicit" "$ROOT/$dest"
    echo "Copied $explicit -> $dest"
    return 0
  fi
  if [ -f "$HOME/Downloads/$src" ]; then
    cp "$HOME/Downloads/$src" "$ROOT/$dest"
    echo "Copied ~/Downloads/$src -> $dest"
    return 0
  fi
  if [ -n "$BUCKET" ] && command -v gsutil >/dev/null 2>&1; then
    if gsutil cp "$BUCKET/$src" "$ROOT/$dest" 2>/dev/null; then
      echo "Downloaded $BUCKET/$src -> $dest"
      return 0
    fi
  fi
  if [ "$required" = "1" ]; then
    echo "Missing $dest and no $src found." >&2
    echo "Put your $src (from your private config store) in ~/Downloads," >&2
    echo "or point KHOJ_ENV_DOCKER at it / set KHOJ_SETUP_BUCKET, then re-run." >&2
    exit 1
  fi
  echo "Note: $dest not found (optional, skipped) — place $src in ~/Downloads if you want it."
  return 0
}
place_env env.docker .env 1 "${KHOJ_ENV_DOCKER:-}"
place_env env.local .env.local 0 "${KHOJ_ENV_LOCAL:-}"
chmod 600 "$ROOT/.env" 2>/dev/null || true

# Ports (defaults mirror docker-compose.yml; .env may override)
KHOJ_PORT="$(grep -E '^KHOJ_PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2)"; KHOJ_PORT="${KHOJ_PORT:-8081}"
FRONTEND_PORT="$(grep -E '^OPENRAG_FRONTEND_PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2)"; FRONTEND_PORT="${FRONTEND_PORT:-3000}"
BACKEND_PORT="$(grep -E '^OPENRAG_BACKEND_PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2)"; BACKEND_PORT="${BACKEND_PORT:-8000}"
LANGFLOW_PORT="$(grep -E '^LANGFLOW_PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2)"; LANGFLOW_PORT="${LANGFLOW_PORT:-7860}"
DASHBOARDS_PORT="$(grep -E '^DASHBOARDS_PORT=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2)"; DASHBOARDS_PORT="${DASHBOARDS_PORT:-5601}"

# --- 3. Bring the stack up --------------------------------------------------
echo "Starting the full stack (first boot pulls/builds several GB — be patient)…"
"${COMPOSE[@]}" up --build -d || { echo "docker compose up failed" >&2; exit 1; }

# --- 4. Wait until the pieces answer ---------------------------------------
wait_url() { # wait_url <url> <name> <tries>
  local url="$1" name="$2" tries="$3" i
  for i in $(seq 1 "$tries"); do
    if curl -s -o /dev/null --max-time 3 "$url"; then
      echo "$name is up: $url"
      return 0
    fi
    sleep 5
  done
  echo "WARNING: $name did not answer at $url — check: docker compose logs $name" >&2
  return 1
}
wait_url "http://localhost:$BACKEND_PORT/provider/health" "OpenRAG backend" 72 || true
wait_url "http://localhost:$FRONTEND_PORT" "OpenRAG UI" 36 || true
wait_url "http://localhost:$KHOJ_PORT/api/config" "Khoj" 36 || true

echo
echo "=================================================="
echo " Khoj full local stack is running:"
echo "   Khoj:          http://localhost:$KHOJ_PORT"
echo "   OpenRAG UI:    http://localhost:$FRONTEND_PORT"
echo "   OpenRAG API:   http://localhost:$BACKEND_PORT"
echo "   Langflow:      http://localhost:$LANGFLOW_PORT"
echo "   Dashboards:    http://localhost:$DASHBOARDS_PORT"
echo " First run: register on the Khoj sign-in page — the"
echo " super-admin email in .env becomes platform admin."
echo " Logs:  bash scripts/run-local-stack.sh logs"
echo " Stop:  bash scripts/run-local-stack.sh down   (data kept;"
echo "        'docker compose down -v' wipes everything)"
echo "=================================================="
