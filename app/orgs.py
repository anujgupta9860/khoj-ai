"""Businesses (organizations) for Khoj.ai.

Enrollment model: an org admin enrolls the business, configures its
branding, and invites employees by email. Invitees accept from inside
the app (matched by account email) or via a one-time invite link the
admin shares. Every member shares the business corpus
(structData.org_id); personal corpora (structData.user_id) stay private.

Roles: admin (enroller; branding, invitations, uploads, members) and
member (search the business corpus). An org must always keep at least
one admin.

Firestore layout:
  orgs/{orgId}                    {name, slug, createdBy, branding{...}}
  orgs/{orgId}/members/{uid}      {uid, email, role}
  orgs/{orgId}/invites/{inviteId}  {email, role, invitedBy, status,
                                    secretHash}
"""

import re

ROLES = ("admin", "member")
UPLOAD_ROLES = ("admin",)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"


class FirestoreOrgService:
    def __init__(self, db):
        self.db = db

    def _org(self, org_id):
        return self.db.collection("orgs").document(org_id)

    # -- orgs ------------------------------------------------------------
    def create_org(self, name: str, owner: dict) -> dict:
        ref = self.db.collection("orgs").document()
        slug = f"{slugify(name)}-{ref.id[:6].lower()}"
        ref.set({"name": name, "slug": slug, "createdBy": owner["uid"], "branding": {}})
        ref.collection("members").document(owner["uid"]).set(
            {"uid": owner["uid"], "email": owner.get("email", ""), "role": "admin"}
        )
        return {"id": ref.id, "name": name, "slug": slug, "role": "admin", "branding": {}}

    def get_org(self, org_id: str):
        snap = self._org(org_id).get()
        if not snap.exists:
            return None
        d = snap.to_dict()
        return {
            "id": org_id,
            "name": d.get("name", ""),
            "slug": d.get("slug", ""),
            "branding": d.get("branding", {}) or {},
        }

    def get_org_by_slug(self, slug: str):
        hits = self.db.collection("orgs").where("slug", "==", slug).limit(1).get()
        return self.get_org(hits[0].id) if hits else None

    def set_branding(self, org_id: str, branding: dict) -> dict:
        self._org(org_id).update({"branding": branding})
        return branding

    # -- members -----------------------------------------------------------
    def membership(self, uid: str, org_id: str):
        snap = self._org(org_id).collection("members").document(uid).get()
        return snap.to_dict().get("role") if snap.exists else None

    def list_orgs_for_user(self, uid: str) -> list:
        out = []
        query = self.db.collection_group("members").where("uid", "==", uid)
        for doc in query.stream():
            org = self.get_org(doc.reference.parent.parent.id)
            if org:
                out.append({**org, "role": doc.to_dict().get("role", "member")})
        return out

    def list_members(self, org_id: str) -> list:
        return [
            {"uid": d.id, **{k: d.to_dict().get(k, "") for k in ("email", "role")}}
            for d in self._org(org_id).collection("members").stream()
        ]

    def add_member(self, org_id: str, target: dict, role: str = "member") -> dict:
        self._org(org_id).collection("members").document(target["uid"]).set(
            {"uid": target["uid"], "email": target.get("email", ""), "role": role}
        )
        return {"uid": target["uid"], "email": target.get("email", ""), "role": role}

    def remove_member(self, org_id: str, uid: str) -> None:
        self._org(org_id).collection("members").document(uid).delete()

    # -- invites -----------------------------------------------------------
    def create_invite(self, org_id: str, email: str, role: str, invited_by: str, secret_hash: str) -> dict:
        ref = self._org(org_id).collection("invites").document()
        data = {
            "email": email,
            "role": role,
            "invitedBy": invited_by,
            "status": "pending",
            "secretHash": secret_hash,
        }
        ref.set(data)
        return {"id": ref.id, **data}

    def get_invite(self, org_id: str, invite_id: str):
        snap = self._org(org_id).collection("invites").document(invite_id).get()
        return {"id": invite_id, **snap.to_dict()} if snap.exists else None

    def list_invites(self, org_id: str) -> list:
        return [
            {"id": d.id, **{k: d.to_dict().get(k, "") for k in ("email", "role", "status", "invitedBy")}}
            for d in self._org(org_id).collection("invites").stream()
        ]

    def set_invite_status(self, org_id: str, invite_id: str, status: str) -> None:
        self._org(org_id).collection("invites").document(invite_id).update({"status": status})

    def invites_for_email(self, email: str) -> list:
        out = []
        query = self.db.collection_group("invites").where("email", "==", email)
        for doc in query.stream():
            data = doc.to_dict()
            if data.get("status") != "pending":
                continue
            org_id = doc.reference.parent.parent.id
            org = self.get_org(org_id)
            out.append({
                "orgId": org_id,
                "inviteId": doc.id,
                "orgName": org["name"] if org else "",
                "role": data.get("role", "member"),
            })
        return out


class InMemoryOrgService:
    """Test double with the same interface."""

    def __init__(self):
        self.orgs = {}
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
            "branding": {},
            "members": {
                owner["uid"]: {"uid": owner["uid"], "email": owner.get("email", ""), "role": "admin"}
            },
            "invites": {},
        }
        return {"id": org_id, "name": name, "slug": slug, "role": "admin", "branding": {}}

    def get_org(self, org_id: str):
        org = self.orgs.get(org_id)
        if not org:
            return None
        return {"id": org_id, "name": org["name"], "slug": org["slug"], "branding": org["branding"]}

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
