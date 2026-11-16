#!/usr/bin/env python3
"""Dump the Jira create-screen metadata Khoj sees for the configured
project + issue type: each field's key, display name, required flag
and schema type.

Use it when a ticket create fails over a field the form never
prompted for (e.g. one demanded only by a workflow validator, like
"Program Stream"): /api/jira/fields can only prompt for fields that
appear here, so this shows whether the field is in the metadata at
all, and under what key.

Config comes from JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN /
JIRA_PROJECT_KEY / JIRA_ISSUE_TYPE in the environment, falling back
to the repo's .env / .env.local files. The token is never printed.

Usage:
    python3 tools/jira_field_dump.py [substring-filter]

With a filter, only fields whose name contains it are listed (the
total count is always printed). Without one, every field is listed.
"""
import os
import sys

import requests


def _load_env_files():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in (".env", ".env.local"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main():
    _load_env_files()
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    project = os.environ.get("JIRA_PROJECT_KEY", "")
    issue_type = os.environ.get("JIRA_ISSUE_TYPE", "Task")
    if not (base and email and token and project):
        print("JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN / JIRA_PROJECT_KEY "
              "are not all set (env or .env)", file=sys.stderr)
        return 2
    resp = requests.get(
        f"{base}/rest/api/3/issue/createmeta",
        params={
            "projectKeys": project,
            "issuetypeNames": issue_type,
            "expand": "projects.issuetypes.fields",
        },
        auth=(email, token),
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"createmeta returned {resp.status_code}: {resp.text[:300]}",
              file=sys.stderr)
        return 1
    projects = (resp.json() or {}).get("projects") or []
    fields = {}
    if projects:
        types = projects[0].get("issuetypes") or []
        if types:
            fields = types[0].get("fields") or {}
    print(f"project={project} issuetype={issue_type} total fields: {len(fields)}")
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    for key in sorted(fields):
        meta = fields[key]
        name = meta.get("name") or ""
        if needle and needle not in name.lower():
            continue
        schema = meta.get("schema") or {}
        print(f"{key:24} {name!r:40} required={meta.get('required')} "
              f"type={schema.get('type')} custom={schema.get('custom', '')} "
              f"allowedValues={len(meta.get('allowedValues') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
