"""Businesses (organizations) for Khoj.ai.

Enrollment model: an org admin enrolls the business, configures its
branding, and invites employees by email. Invitees accept from inside
the app (matched by account email) or via a one-time invite link the
admin shares. Every member shares the business corpus
(structData.org_id); personal corpora (structData.user_id) stay private.

Roles: admin (enroller; branding, invitations, uploads, members) and
member (search the business corpus). An org must always keep at least
one admin.

Storage: SQLite (SqliteOrgService below) — orgs, members, invites,
uploads and events tables in <KHOJ_DATA_DIR>/khoj.db. Records cross the
service boundary as the same dicts the old Firestore layout used:
  orgs        {name, slug, createdBy, branding{...}, status}
  members     {uid, email, role}
  invites     {email, role, invitedBy, status, secretHash}
"""

import json
import re
import time
import uuid

ROLES = ("admin", "member")
UPLOAD_ROLES = ("admin",)

# The platform itself is an org: Khoj.ai. Its admins are the super admins
# for every org and user on the platform (approvals, all conversations,
# RAG metrics). Seeded by scripts/set_super_admin.py.
PLATFORM_ORG_ID = "khoj"
ORG_STATUSES = ("pending", "approved", "rejected")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"


class SqliteOrgService:
    """The org service on SQLite (app/db.py). Same interface the
    Firestore implementation had; records are JSON documents in typed
    tables so the dict shapes crossing the API never change."""

    def __init__(self, connect):
        self._connect = connect
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orgs (
                    org_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS members (
                    org_id TEXT NOT NULL, uid TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (org_id, uid));
                CREATE TABLE IF NOT EXISTS invites (
                    org_id TEXT NOT NULL, invite_id TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY (org_id, invite_id));
                CREATE TABLE IF NOT EXISTS uploads (
                    doc_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    data TEXT NOT NULL);
                """
            )

    # -- orgs ------------------------------------------------------------
    def _org_row(self, conn, org_id):
        row = conn.execute(
            "SELECT data FROM orgs WHERE org_id = ?", (org_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def create_org(self, name: str, owner: dict) -> dict:
        org_id = uuid.uuid4().hex[:20]
        slug = f"{slugify(name)}-{org_id[:6].lower()}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO orgs (org_id, data) VALUES (?, ?)",
                (org_id, json.dumps({
                    "name": name, "slug": slug, "createdBy": owner["uid"],
                    "branding": {}, "status": "pending",
                })),
            )
            conn.execute(
                "INSERT INTO members (org_id, uid, data) VALUES (?, ?, ?)",
                (org_id, owner["uid"], json.dumps({
                    "uid": owner["uid"], "email": owner.get("email", ""),
                    "role": "admin",
                })),
            )
        return {"id": org_id, "name": name, "slug": slug, "role": "admin",
                "branding": {}, "status": "pending"}

    def get_org(self, org_id: str):
        with self._connect() as conn:
            d = self._org_row(conn, org_id)
        if d is None:
            return None
        return {
            "id": org_id,
            "name": d.get("name", ""),
            "slug": d.get("slug", ""),
            "branding": d.get("branding", {}) or {},
            "status": d.get("status", "approved"),
            "createdBy": d.get("createdBy", ""),
        }

    def get_org_by_slug(self, slug: str):
        with self._connect() as conn:
            rows = conn.execute("SELECT org_id, data FROM orgs").fetchall()
        for org_id, raw in rows:
            if json.loads(raw).get("slug") == slug:
                return self.get_org(org_id)
        return None

    def set_branding(self, org_id: str, branding: dict) -> dict:
        with self._connect() as conn:
            d = self._org_row(conn, org_id) or {}
            d["branding"] = branding
            conn.execute(
                "UPDATE orgs SET data = ? WHERE org_id = ?",
                (json.dumps(d), org_id),
            )
        return branding

    # -- members -----------------------------------------------------------
    def membership(self, uid: str, org_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM members WHERE org_id = ? AND uid = ?",
                (org_id, uid),
            ).fetchone()
        return json.loads(row[0]).get("role") if row else None

    def list_orgs_for_user(self, uid: str) -> list:
        out = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT org_id, data FROM members WHERE uid = ?", (uid,)
            ).fetchall()
        for org_id, raw in rows:
            org = self.get_org(org_id)
            if org:
                out.append({**org, "role": json.loads(raw).get("role", "member")})
        return out

    def list_members(self, org_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT uid, data FROM members WHERE org_id = ?", (org_id,)
            ).fetchall()
        return [
            {"uid": uid, **{k: json.loads(raw).get(k, "") for k in ("email", "role")}}
            for uid, raw in rows
        ]

    def add_member(self, org_id: str, target: dict, role: str = "member") -> dict:
        member = {"uid": target["uid"], "email": target.get("email", ""), "role": role}
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO members (org_id, uid, data) VALUES (?, ?, ?)",
                (org_id, target["uid"], json.dumps(member)),
            )
        return member

    def remove_member(self, org_id: str, uid: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM members WHERE org_id = ? AND uid = ?", (org_id, uid)
            )

    # -- platform / status / events -----------------------------------------
    def ensure_platform_org(self, owner: dict) -> dict:
        with self._connect() as conn:
            if self._org_row(conn, PLATFORM_ORG_ID) is None:
                conn.execute(
                    "INSERT INTO orgs (org_id, data) VALUES (?, ?)",
                    (PLATFORM_ORG_ID, json.dumps({
                        "name": "Khoj.ai", "slug": "khoj",
                        "createdBy": owner["uid"], "branding": {},
                        "status": "approved",
                    })),
                )
            conn.execute(
                "INSERT OR REPLACE INTO members (org_id, uid, data) VALUES (?, ?, ?)",
                (PLATFORM_ORG_ID, owner["uid"], json.dumps({
                    "uid": owner["uid"], "email": owner.get("email", ""),
                    "role": "admin",
                })),
            )
        return self.get_org(PLATFORM_ORG_ID)

    def list_orgs_by_status(self, status: str) -> list:
        out = []
        with self._connect() as conn:
            rows = conn.execute("SELECT org_id FROM orgs").fetchall()
        for (org_id,) in rows:
            org = self.get_org(org_id)
            if org and org["status"] == status:
                out.append(org)
        return out

    def set_org_status(self, org_id: str, status: str) -> None:
        with self._connect() as conn:
            d = self._org_row(conn, org_id) or {}
            d["status"] = status
            conn.execute(
                "UPDATE orgs SET data = ? WHERE org_id = ?",
                (json.dumps(d), org_id),
            )

    def log_event(self, entry: dict) -> None:
        doc = dict(entry)
        doc.setdefault("ts", time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, data) VALUES (?, ?)",
                (float(doc["ts"]), json.dumps(doc)),
            )

    def list_events(self, limit: int = 200) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # -- file uploads (ingestion status tracking) ---------------------------
    def record_upload(self, rec: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO uploads (doc_id, scope_key, data)"
                " VALUES (?, ?, ?)",
                (rec["docId"], rec.get("scopeKey", ""), json.dumps(rec)),
            )

    def list_uploads(self, scope_key: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM uploads WHERE scope_key = ?", (scope_key,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_all_uploads(self, limit: int = 1000) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM uploads LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    # -- invites -----------------------------------------------------------
    def create_invite(self, org_id: str, email: str, role: str, invited_by: str, secret_hash: str) -> dict:
        invite_id = uuid.uuid4().hex[:20]
        data = {
            "email": email, "role": role, "invitedBy": invited_by,
            "status": "pending", "secretHash": secret_hash,
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invites (org_id, invite_id, data) VALUES (?, ?, ?)",
                (org_id, invite_id, json.dumps(data)),
            )
        return {"id": invite_id, **data}

    def get_invite(self, org_id: str, invite_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM invites WHERE org_id = ? AND invite_id = ?",
                (org_id, invite_id),
            ).fetchone()
        return {"id": invite_id, **json.loads(row[0])} if row else None

    def list_invites(self, org_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT invite_id, data FROM invites WHERE org_id = ?", (org_id,)
            ).fetchall()
        return [
            {"id": iid, **{k: json.loads(raw).get(k, "") for k in ("email", "role", "status", "invitedBy")}}
            for iid, raw in rows
        ]

    def set_invite_status(self, org_id: str, invite_id: str, status: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM invites WHERE org_id = ? AND invite_id = ?",
                (org_id, invite_id),
            ).fetchone()
            if row:
                d = json.loads(row[0])
                d["status"] = status
                conn.execute(
                    "UPDATE invites SET data = ? WHERE org_id = ? AND invite_id = ?",
                    (json.dumps(d), org_id, invite_id),
                )

    def invites_for_email(self, email: str) -> list:
        out = []
        with self._connect() as conn:
            rows = conn.execute("SELECT org_id, invite_id, data FROM invites").fetchall()
        for org_id, invite_id, raw in rows:
            data = json.loads(raw)
            if data.get("email") != email or data.get("status") != "pending":
                continue
            org = self.get_org(org_id)
            out.append({
                "orgId": org_id,
                "inviteId": invite_id,
                "orgName": org["name"] if org else "",
                "role": data.get("role", "member"),
            })
        return out


class InMemoryOrgService:
    """Test double with the same interface."""

    def __init__(self):
        self.orgs = {}
        self._events = []
        self.uploads = {}
        self._seq = 0

    def _new_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    def create_org(self, name: str, owner: dict) -> dict:
        org_id = self._new_id("org")
        slug = f"{slugify(name)}-{org_id[-6:].lower()}"
        self.orgs[org_id] = {
            "name": name,
            "slug": slug,
            "status": "pending",
            "branding": {},
            "members": {
                owner["uid"]: {"uid": owner["uid"], "email": owner.get("email", ""), "role": "admin"}
            },
            "invites": {},
        }
        return {"id": org_id, "name": name, "slug": slug, "role": "admin",
                "branding": {}, "status": "pending"}

    def get_org(self, org_id: str):
        org = self.orgs.get(org_id)
        if not org:
            return None
        return {"id": org_id, "name": org["name"], "slug": org["slug"],
                "branding": org["branding"], "status": org.get("status", "approved"),
                "createdBy": org.get("createdBy", "")}

    def get_org_by_slug(self, slug: str):
        for org_id, org in self.orgs.items():
            if org["slug"] == slug:
                return self.get_org(org_id)
        return None

    def set_branding(self, org_id: str, branding: dict) -> dict:
        self.orgs[org_id]["branding"] = branding
        return branding

    def membership(self, uid: str, org_id: str):
        org = self.orgs.get(org_id)
        member = org["members"].get(uid) if org else None
        return member["role"] if member else None

    def list_orgs_for_user(self, uid: str) -> list:
        return [
            {**self.get_org(oid), "role": o["members"][uid]["role"]}
            for oid, o in self.orgs.items()
            if uid in o["members"]
        ]

    def list_members(self, org_id: str) -> list:
        return list(self.orgs[org_id]["members"].values())

    def add_member(self, org_id: str, target: dict, role: str = "member") -> dict:
        member = {"uid": target["uid"], "email": target.get("email", ""), "role": role}
        self.orgs[org_id]["members"][target["uid"]] = member
        return member

    def remove_member(self, org_id: str, uid: str) -> None:
        self.orgs.get(org_id, {}).get("members", {}).pop(uid, None)

    def ensure_platform_org(self, owner: dict) -> dict:
        if PLATFORM_ORG_ID not in self.orgs:
            self.orgs[PLATFORM_ORG_ID] = {
                "name": "Khoj.ai", "slug": "khoj", "status": "approved",
                "branding": {}, "members": {}, "invites": {},
            }
        self.orgs[PLATFORM_ORG_ID]["members"][owner["uid"]] = {
            "uid": owner["uid"], "email": owner.get("email", ""), "role": "admin",
        }
        return self.get_org(PLATFORM_ORG_ID)

    def list_orgs_by_status(self, status: str) -> list:
        return [
            self.get_org(oid) for oid, o in self.orgs.items()
            if o.get("status", "approved") == status
        ]

    def set_org_status(self, org_id: str, status: str) -> None:
        self.orgs[org_id]["status"] = status

    def log_event(self, entry: dict) -> None:
        doc = dict(entry)
        doc.setdefault("ts", time.time())
        self._events.append(doc)

    def list_events(self, limit: int = 200) -> list:
        return sorted(self._events, key=lambda e: e["ts"], reverse=True)[:limit]

    def record_upload(self, rec: dict) -> None:
        self.uploads[rec["docId"]] = dict(rec)

    def list_uploads(self, scope_key: str) -> list:
        return [dict(r) for r in self.uploads.values() if r.get("scopeKey") == scope_key]

    def list_all_uploads(self, limit: int = 1000) -> list:
        return [dict(r) for r in self.uploads.values()][:limit]

    def create_invite(self, org_id: str, email: str, role: str, invited_by: str, secret_hash: str) -> dict:
        invite_id = self._new_id("inv")
        invite = {
            "id": invite_id,
            "email": email,
            "role": role,
            "invitedBy": invited_by,
            "status": "pending",
            "secretHash": secret_hash,
        }
        self.orgs[org_id]["invites"][invite_id] = invite
        return invite

    def get_invite(self, org_id: str, invite_id: str):
        return self.orgs.get(org_id, {}).get("invites", {}).get(invite_id)

    def list_invites(self, org_id: str) -> list:
        return [
            {k: inv.get(k, "") for k in ("id", "email", "role", "status", "invitedBy")}
            for inv in self.orgs[org_id]["invites"].values()
        ]

    def set_invite_status(self, org_id: str, invite_id: str, status: str) -> None:
        self.orgs[org_id]["invites"][invite_id]["status"] = status

    def invites_for_email(self, email: str) -> list:
        out = []
        for org_id, org in self.orgs.items():
            for invite_id, inv in org["invites"].items():
                if inv["email"] == email and inv["status"] == "pending":
                    out.append({
                        "orgId": org_id,
                        "inviteId": invite_id,
                        "orgName": org["name"],
                        "role": inv["role"],
                    })
        return out
