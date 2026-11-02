"""Make a user a Khoj.ai super admin.

Super admins are the admins of the platform org ("khoj"): they approve
org signups, see all search conversations, and read the RAG metrics.
This script seeds the platform org (idempotent) and adds the user to it
as admin.

Run anywhere with Application Default Credentials for the project:
    python3 scripts/set_super_admin.py someone@example.com
The user must already have a Khoj account (sign up on the site first).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import firebase_admin
from firebase_admin import auth, firestore

from app.orgs import FirestoreOrgService

firebase_admin.initialize_app()
user = auth.get_user_by_email(sys.argv[1])
svc = FirestoreOrgService(firestore.client())
org = svc.ensure_platform_org({"uid": user.uid, "email": user.email})
print(f"super admin: {user.email} ({user.uid}) -> platform org {org['id']} ({org['name']})")
