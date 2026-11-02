"""Grant the Khoj.ai admin claim to a user by email.

Run anywhere with Application Default Credentials for the project
(e.g. on the VM):  python3 scripts/set_admin.py someone@example.com
The user picks up the claim at their next sign-in / token refresh.
"""

import sys

import firebase_admin
from firebase_admin import auth

firebase_admin.initialize_app()
user = auth.get_user_by_email(sys.argv[1])
auth.set_custom_user_claims(user.uid, {"admin": True})
print(f"admin granted to {user.email} ({user.uid})")
