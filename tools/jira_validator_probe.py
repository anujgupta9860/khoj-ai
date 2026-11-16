#!/usr/bin/env python3
"""Probe a validator-only Jira rejection with byte-exact output.

Sends one create that satisfies every field createmeta flags as
required (first allowed value for Components / Affects versions),
so the only possible 400 left is a workflow-validator demand (e.g.
"Program Stream"). Prints the status, the first error message with
non-ASCII characters escaped (a curly apostrophe shows as \\u2019),
and what the app's _jira_missing_prompts() resolves from the real
response. The create is expected to fail, so no ticket is created.

Runs INSIDE the khoj container:

    docker exec -i khoj python - < tools/jira_validator_probe.py
"""
import requests

from app import main


def run():
    cfg = main._jira_config()
    if not cfg:
        print("Jira is not configured in this environment")
        return 2
    meta = main._jira_createmeta_fields(cfg)
    fields = {
        "project": {"key": cfg["project"]},
        "summary": "field probe",
        "issuetype": {"name": cfg["issue_type"]},
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": "probe"}]}],
        },
    }
    # Satisfy the createmeta-required list fields with their first
    # allowed value so only validator demands can fail the create.
    for key in ("components", "versions"):
        allowed = (meta.get(key) or {}).get("allowedValues") or []
        if allowed:
            name = allowed[0].get("name") or allowed[0].get("value")
            if name:
                fields[key] = [{"name": name}]
    r = requests.post(
        cfg["base"] + "/rest/api/3/issue",
        json={"fields": fields},
        auth=(cfg["email"], cfg["token"]),
        timeout=20,
    )
    print("status", r.status_code)
    try:
        msg = (r.json().get("errorMessages") or [""])[0]
    except Exception:
        msg = ""
    print("msg", msg.encode("unicode_escape").decode())
    print("missing", main._jira_missing_prompts(cfg, r, fields, {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
