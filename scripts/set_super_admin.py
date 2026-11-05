"""Make a user a Khoj.ai super admin.

Super admins are the admins of the platform org ("khoj"): they approve
org signups, see all search conversations, and read the RAG metrics.
This script seeds the platform org (idempotent) and adds the user to it
as admin. (Registering with an email listed in KHOJ_SUPER_ADMIN_EMAIL
does the same automatically at sign-in.)

Run against the deployment's data dir:
    KHOJ_DATA_DIR=/path/to/data python3 scripts/set_super_admin.py someone@example.com
The user must already have a Khoj account (register on the site first).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import Users, connect
from app.orgs import SqliteOrgService

if len(sys.argv) != 2:
    sys.exit("usage: set_super_admin.py <account email>")

user = Users().get_by_email(sys.argv[1])
if user is None:
    sys.exit(f"no Khoj account with email {sys.argv[1]} - register first")

svc = SqliteOrgService(connect)
org = svc.ensure_platform_org(user)
print(f"super admin: {user['email']} ({user['uid']}) -> platform org {org['id']} ({org['name']})")
