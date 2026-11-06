#!/usr/bin/env python3
"""One-shot OpenRAG onboarding for the local Docker stack.

The OpenRAG backend keeps provider/model config server-side: env vars
alone never finish onboarding, so this script performs the same calls
the setup tooling uses once the backend is reachable:

  1. POST /onboarding          provider + chat/embedding models (+ key)
  2. POST /onboarding/state     {"current_step": 4} — closes the wizard
  3. POST /settings             OCR on, chunks 1000/200 (the GKE config)

It is best-effort by design: every step logs what happened and the
script still exits 0 when a step is skipped or rejected, so a failed
onboarding never blocks the rest of the stack from starting. Re-run
any time with:  docker compose run --rm openrag-init
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("OPENRAG_URL", "http://openrag-backend:8000").rstrip("/")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "openai")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OLLAMA_ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")


def call(method, path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # connection refused while the backend boots
        return None, repr(e)


def wait_for_backend(deadline_s=900):
    print(f"[init] waiting for OpenRAG backend at {BASE} ...", flush=True)
    start = time.time()
    while time.time() - start < deadline_s:
        status, _ = call("GET", "/provider/health", timeout=10)
        if status is not None:
            print(f"[init] backend is responding (health -> {status})", flush=True)
            return True
        time.sleep(10)
    print("[init] ERROR: backend never became reachable; skipping onboarding", flush=True)
    return False


def main():
    if not wait_for_backend():
        return 0

    payload = {
        "llm_provider": LLM_PROVIDER,
        "llm_model": LLM_MODEL,
        "embedding_provider": EMBEDDING_PROVIDER,
        "embedding_model": EMBEDDING_MODEL,
    }
    if LLM_PROVIDER == "openai" or EMBEDDING_PROVIDER == "openai":
        if OPENAI_API_KEY:
            payload["openai_api_key"] = OPENAI_API_KEY
        else:
            print("[init] WARNING: OPENAI_API_KEY is empty — OpenAI onboarding "
                  "will fail until you set it in .env and re-run this init.", flush=True)
    if LLM_PROVIDER == "ollama" or EMBEDDING_PROVIDER == "ollama":
        payload["ollama_endpoint"] = OLLAMA_ENDPOINT or "http://host.docker.internal:11434"

    creds = {}
    if OPENAI_BASE_URL and (LLM_PROVIDER == "openai" or EMBEDDING_PROVIDER == "openai"):
        # OpenRAG takes a custom endpoint as the provider's api_base
        # credential; it then applies to chat AND embeddings.
        creds.setdefault("openai", {})["api_base"] = OPENAI_BASE_URL
        print(f"[init] custom OpenAI base URL: {OPENAI_BASE_URL}", flush=True)
    # Any other LiteLLM-native provider (e.g. "gemini" for Google AI
    # Studio) takes its key as a provider credential. OPENAI_API_KEY
    # doubles as the key slot: in the Gemini recipe it holds the AI
    # Studio key. The provider NAME + bare model names keep the stored
    # embedding-space id identical to the route search resolves.
    for provider in {LLM_PROVIDER, EMBEDDING_PROVIDER}:
        if provider not in ("openai", "ollama"):
            if OPENAI_API_KEY:
                creds.setdefault(provider, {})["api_key"] = OPENAI_API_KEY
            else:
                print(f"[init] WARNING: no key set for provider {provider} — "
                      "onboarding will fail until you set OPENAI_API_KEY "
                      "in .env and re-run this init.", flush=True)
    if creds:
        payload["provider_credentials"] = creds

    print(f"[init] onboarding: llm={LLM_PROVIDER}/{LLM_MODEL} "
          f"embedding={EMBEDDING_PROVIDER}/{EMBEDDING_MODEL}", flush=True)
    # Onboarding validates the chat model (tool-calling probe against the
    # provider), which can take a while on a cold stack — retry a few times.
    status = None
    for attempt in range(1, 7):
        status, body = call("POST", "/onboarding", payload, timeout=300)
        if status and status < 300:
            print(f"[init] onboarding accepted (attempt {attempt})", flush=True)
            break
        print(f"[init] onboarding attempt {attempt} -> {status}: {body[:300]}", flush=True)
        time.sleep(20)
    else:
        print("[init] WARNING: onboarding did not complete — the OpenRAG UI "
              "wizard can finish it, or fix the cause and re-run openrag-init.", flush=True)

    status, body = call("POST", "/onboarding/state", {"current_step": 4})
    print(f"[init] onboarding state -> {status}", flush=True)

    status, body = call("POST", "/settings",
                        {"ocr": True, "chunk_size": 1000, "chunk_overlap": 200})
    if status and status < 300:
        print("[init] knowledge settings set: OCR on, chunks 1000/200", flush=True)
    else:
        print(f"[init] NOTE: settings update -> {status} ({body[:200]}); "
              "OCR/chunking can be adjusted later in the OpenRAG UI.", flush=True)

    status, body = call("GET", "/provider/health")
    print(f"[init] provider health -> {status}: {body[:300]}", flush=True)
    print("[init] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
