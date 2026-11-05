"""SQLite storage for Khoj — the only database now.

One file, <KHOJ_DATA_DIR>/khoj.db (WAL mode). Holds the user accounts
(this module) plus the org/upload/event tables owned by
app.orgs.SqliteOrgService. Short-lived connections per call keep the
web service and any local tooling safe against each other's writes at
POC scale.
"""

import os
import sqlite3
import time
import uuid

from . import auth

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    pw_salt TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def db_path() -> str:
    return os.path.join(auth.data_dir(), "khoj.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    return conn


class Users:
    """Email/password accounts (see app/auth.py for hashing + tokens)."""

    def create_user(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        pw_hash, pw_salt = auth.hash_password(password)
        uid = "u-" + uuid.uuid4().hex[:24]
        with connect() as conn:
            conn.execute(
                "INSERT INTO users (uid, email, pw_hash, pw_salt, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (uid, email, pw_hash, pw_salt, int(time.time())),
            )
        return {"uid": uid, "email": email}

    def get_by_email(self, email: str) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT uid, email FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def get_by_uid(self, uid: str) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT uid, email FROM users WHERE uid = ?", (uid,)
            ).fetchone()
        return dict(row) if row else None

    def authenticate(self, email: str, password: str) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT uid, email, pw_hash, pw_salt FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        if row is None:
            return None
        if not auth.check_password(password, row["pw_hash"], row["pw_salt"]):
            return None
        return {"uid": row["uid"], "email": row["email"]}
