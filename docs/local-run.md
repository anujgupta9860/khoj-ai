# Run Khoj locally (end to end)

One script asks for every config value and starts the whole product on
your machine — web app + A2A chat agent — pointed at an OpenRAG you
choose.

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

## What you need

- Python 3.10+.
- No accounts or credentials to prepare: auth is built in and all
  data lives in the data dir you choose. Only if you pick
  `KHOJ_STORAGE=gcs` do you need Google credentials (gcloud ADC or a
  service-account key) for the originals bucket.
- A running OpenRAG to point `OPENRAG_URL` at — a local backend
  (`http://localhost:8000`, e.g. the openrag-gemma-local stack) or the
  deployed GKE stack through its app proxy
  (`http://34.102.202.103/api`; Khoj appends `/search`, `/files`, …
  and the proxy strips the `/api` prefix).
- Optional: an OpenAI key for the chat agent (blank = extractive
  answers), and Jira settings + API token for the 🎫 ticket button
  (blank token = feature off). Every Atlassian value the script asks
  for mirrors the Helm values of the same name.
