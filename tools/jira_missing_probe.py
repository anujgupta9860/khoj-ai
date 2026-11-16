#!/usr/bin/env python3
"""Probe how the app resolves a real Jira create rejection.

Sends one minimal create to the configured Jira project (it is
expected to fail with a 400 - e.g. a workflow validator demanding a
field like "Program Stream" - so no ticket is created), prints the
raw response body byte-for-byte, then runs the app's own
_jira_missing_prompts() over it. If the printed "missing" list is
empty while the body names a field, the resolver's patterns do not
match Jira's actual message text.

Runs INSIDE the khoj container, where the app's dependencies and
the JIRA_* env config live:

    docker exec -i khoj python - < tools/jira_missing_probe.py
"""
import requests

from app import main


def run():
    cfg = main._jira_config()
    if not cfg:
        print("Jira is not configured in this environment")
        return 2
    payload = {"fields": {
        "project": {"key": cfg["project"]},
        "summary": "field probe",
        "issuetype": {"name": cfg["issue_type"]},
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": "probe"}]}],
        },
    }}
    r = requests.post(
        cfg["base"] + "/rest/api/3/issue",
        json=payload,
        auth=(cfg["email"], cfg["token"]),
        timeout=20,
    )
    print("status", r.status_code)
    print("body", repr(r.text[:250]))
    print("missing", main._jira_missing_prompts(cfg, r, payload["fields"], {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
