#!/usr/bin/env bash
# Khoj — run the complete stack locally: web app + A2A chat agent.
#
#   bash scripts/start-local.sh              # interactive setup, then run
#   bash scripts/start-local.sh --setup-only # only ask + write .env.local
#   bash scripts/start-local.sh --yes        # reuse .env.local, no prompts
#
# The script asks for every config value (defaults in [brackets]), saves
# them to .env.local (gitignored, secrets included — keep it private),
# creates .venv-local, installs dependencies, starts both services,
# waits until they answer, and prints the URLs. Ctrl+C stops both.
#
# Khoj needs NO cloud account: accounts and metadata live in SQLite and
# uploaded originals on local disk, under the data dir you choose.
# Only OpenRAG is external — point OPENRAG_URL at a running OpenRAG
# (a local one, or the deployed GKE stack through its app proxy).
set -u
cd "$(dirname "$0")/.."
ROOT="$PWD"
ENV_FILE="$ROOT/.env.local"
VENV="$ROOT/.venv-local"
RUN_DIR="$ROOT/.local-run"
mkdir -p "$RUN_DIR"

SETUP_ONLY=0
ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --setup-only) SETUP_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

ask() { # ask VAR "Prompt" "default" — normal input
  local var="$1" prompt="$2" def="${3:-}" val=""
  if [ -n "$def" ]; then
    read -r -p "$prompt [$def]: " val || true
    val="${val:-$def}"
  else
    read -r -p "$prompt: " val || true
  fi
  printf -v "$var" '%s' "$val"
}
ask_secret() { # ask_secret VAR "Prompt" — hidden input, may be empty
  local var="$1" prompt="$2" val=""
  read -r -s -p "$prompt (hidden, blank = skip): " val || true
  echo
  printf -v "$var" '%s' "$val"
}

write_env() {
  umask 077
  cat > "$ENV_FILE" <<EOF
# Khoj local config — written by scripts/start-local.sh. Do not commit.
WEB_PORT=$WEB_PORT
AGENT_PORT=$AGENT_PORT
KHOJ_DATA_DIR=$KHOJ_DATA_DIR
KHOJ_STORAGE=$KHOJ_STORAGE
KHOJ_BUCKET=$KHOJ_BUCKET
KHOJ_AUTH_SECRET=$KHOJ_AUTH_SECRET
KHOJ_SUPER_ADMIN_EMAIL=$KHOJ_SUPER_ADMIN_EMAIL
OPENRAG_URL=$OPENRAG_URL
OPENAI_API_KEY=$OPENAI_API_KEY
OPENAI_MODEL=$OPENAI_MODEL
JIRA_BASE_URL=$JIRA_BASE_URL
JIRA_EMAIL=$JIRA_EMAIL
JIRA_PROJECT_KEY=$JIRA_PROJECT_KEY
JIRA_ISSUE_TYPE=$JIRA_ISSUE_TYPE
JIRA_API_TOKEN=$JIRA_API_TOKEN
EOF
  echo "Saved config to .env.local"
}

setup_config() {
  echo "== Khoj local setup =="
  echo "Press Enter to accept the [default]. Secrets are typed hidden."
  echo
  ask WEB_PORT "Web port" "8080"
  ask AGENT_PORT "Agent port" "8090"
  echo "-- Khoj data (accounts, orgs, logs: SQLite; originals: storage) --"
  ask KHOJ_DATA_DIR "Data directory" "$ROOT/data"
  ask KHOJ_STORAGE "Originals storage: 'local' (disk) or 'gcs' (Google bucket)" "local"
  KHOJ_BUCKET=""
  if [ "$KHOJ_STORAGE" = "gcs" ]; then
    ask KHOJ_BUCKET "GCS bucket for originals" "khoj-ai-corpus-innovation-lab"
    echo "   (gcs mode uses your Google credentials: gcloud ADC or"
    echo "    GOOGLE_APPLICATION_CREDENTIALS — set the latter in .env.local"
    echo "    by hand if you use a key file.)"
  fi
  echo "-- Built-in auth --"
  ask KHOJ_SUPER_ADMIN_EMAIL "Super-admin email (platform admin at sign-in)" "anuj.gupta9860@gmail.com"
  ask_secret KHOJ_AUTH_SECRET "Token signing secret (blank = generate one)"
  if [ -z "$KHOJ_AUTH_SECRET" ]; then
    KHOJ_AUTH_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    echo "   Generated a signing secret and saved it in .env.local."
  fi
  echo "-- OpenRAG (retrieval engine; must already be running somewhere) --"
  echo "   Local OpenRAG backend:        http://localhost:8000"
  echo "   Deployed GKE stack (via app): http://34.102.202.103/api"
  ask OPENRAG_URL "OpenRAG base URL" "http://localhost:8000"
  echo "-- Chat agent (OpenAI) --"
  ask_secret OPENAI_API_KEY "OpenAI API key for the chat agent"
  ask OPENAI_MODEL "OpenAI model" "gpt-4o-mini"
  echo "-- Jira ticket button (optional; blank token = feature off) --"
  ask JIRA_BASE_URL "Jira site base URL" "https://khoj.atlassian.net"
  ask JIRA_EMAIL "Jira account email" "anuj.gupta9860@gmail.com"
  ask JIRA_PROJECT_KEY "Jira project key" "RAG"
  ask JIRA_ISSUE_TYPE "Jira issue type" "Task"
  ask_secret JIRA_API_TOKEN "Jira API token"
  write_env
}

REUSE=0
if [ -f "$ENV_FILE" ]; then
  if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
    REUSE=1
  else
    read -r -p "Found .env.local — [R]euse it, or re-enter config? [R/e]: " ans || true
    case "${ans:-R}" in
      e|E) REUSE=0 ;;
      *) REUSE=1 ;;
    esac
  fi
fi
if [ "$REUSE" = "0" ]; then
  setup_config
else
  echo "Reusing existing .env.local (delete it or answer 'e' next time to reconfigure)."
fi
if [ "$SETUP_ONLY" = "1" ]; then
  echo "Setup only — not starting services."
  exit 0
fi

# Load config
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export KHOJ_AGENT_URL="http://localhost:${AGENT_PORT}"
export KHOJ_WEB_URL="http://localhost:${WEB_PORT}"
mkdir -p "$KHOJ_DATA_DIR"

# Python env
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv (.venv-local)…"
  python3 -m venv "$VENV" || { echo "python3 -m venv failed" >&2; exit 1; }
fi
echo "Installing dependencies…"
"$VENV/bin/pip" install -q -r requirements.txt -r agent/requirements.txt \
  || { echo "pip install failed" >&2; exit 1; }

# OpenRAG reachability (warn only — UI still loads without it)
if command -v curl >/dev/null 2>&1; then
  if curl -s -o /dev/null --max-time 5 "$OPENRAG_URL/files"; then
    echo "OpenRAG reachable at $OPENRAG_URL"
  else
    echo "WARNING: OpenRAG not reachable at $OPENRAG_URL — search/chat will fail until it is up."
  fi
fi

cleanup() {
  echo
  echo "Stopping Khoj local stack…"
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
  [ -n "${AGENT_PID:-}" ] && kill "$AGENT_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

echo "Starting A2A agent on :$AGENT_PORT (log: .local-run/agent.log)…"
PYTHONPATH="$ROOT" "$VENV/bin/python" -m uvicorn --app-dir agent main:app \
  --host 0.0.0.0 --port "$AGENT_PORT" > "$RUN_DIR/agent.log" 2>&1 &
AGENT_PID=$!

echo "Starting web app on :$WEB_PORT (log: .local-run/web.log)…"
"$VENV/bin/python" -m uvicorn app.main:app \
  --host 0.0.0.0 --port "$WEB_PORT" > "$RUN_DIR/web.log" 2>&1 &
WEB_PID=$!

wait_for() { # wait_for URL NAME PID LOGFILE
  local url="$1" name="$2" pid="$3" logf="$4" i
  for i in $(seq 1 60); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$name exited during startup — last log lines:" >&2
      tail -n 20 "$logf" >&2
      cleanup
    fi
    if curl -s -o /dev/null --max-time 2 "$url"; then
      echo "$name is up: $url"
      return 0
    fi
    sleep 1
  done
  echo "WARNING: $name did not answer at $url within 60s (see $logf)" >&2
}

wait_for "http://localhost:$AGENT_PORT/.well-known/agent-card.json" "Agent" "$AGENT_PID" "$RUN_DIR/agent.log"
wait_for "http://localhost:$WEB_PORT/api/config" "Web" "$WEB_PID" "$RUN_DIR/web.log"

echo
echo "=================================================="
echo " Khoj is running locally:"
echo "   Web UI:  http://localhost:$WEB_PORT"
echo "   Agent:   http://localhost:$AGENT_PORT/.well-known/agent-card.json"
echo "   Data:    $KHOJ_DATA_DIR (storage: $KHOJ_STORAGE)"
echo "   OpenRAG: $OPENRAG_URL"
if [ -n "${JIRA_API_TOKEN:-}" ] && [ -n "${JIRA_BASE_URL:-}" ]; then
  echo "   Jira:    enabled → $JIRA_BASE_URL, project $JIRA_PROJECT_KEY"
else
  echo "   Jira:    disabled (no token configured)"
fi
echo " First run: create your account on the sign-in page — the"
echo " super-admin email above becomes platform admin automatically."
echo " Press Ctrl+C to stop both services."
echo "=================================================="
wait
