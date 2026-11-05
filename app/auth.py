"""Khoj built-in authentication — no Firebase, no external service.

Users register with email + password. Passwords are stored as
PBKDF2-HMAC-SHA256 hashes with per-user salts (stdlib only). Sessions
are HS256 JSON Web Tokens signed with a shared secret, implemented with
the standard library so the web service and the A2A agent verify them
identically with zero extra dependencies.

Secret resolution (must match across web + agent):
  1. env KHOJ_AUTH_SECRET, when set (GKE: External Secrets <- GSM
     `khoj-auth-secret`; local: scripts/start-local.sh generates one
     into .env.local), else
  2. the file <KHOJ_DATA_DIR>/auth_secret, created on first use with
     0600 permissions (single-host runs where both processes share the
     data directory).

Token lifetime: KHOJ_TOKEN_TTL_HOURS (default 168 = 7 days).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

_PBKDF2_ROUNDS = 200_000


def data_dir() -> str:
    path = os.environ.get("KHOJ_DATA_DIR", os.path.join(os.getcwd(), "data"))
    os.makedirs(path, exist_ok=True)
    return path


def auth_secret() -> str:
    env = os.environ.get("KHOJ_AUTH_SECRET", "")
    if env:
        return env
    path = os.path.join(data_dir(), "auth_secret")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value


def hash_password(password: str) -> tuple[str, str]:
    """(hash_hex, salt_hex) for a new password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return digest.hex(), salt.hex()


def check_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            _PBKDF2_ROUNDS,
        )
    except Exception:
        return False
    return hmac.compare_digest(digest.hex(), hash_hex or "")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(uid: str, email: str) -> str:
    ttl = int(os.environ.get("KHOJ_TOKEN_TTL_HOURS", "168"))
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": uid, "email": email, "iat": now, "exp": now + ttl * 3600}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(
        auth_secret().encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(sig)}"


def verify_token(token: str) -> dict:
    """Returns {'uid', 'email'} or raises ValueError."""
    try:
        head_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{head_b64}.{payload_b64}"
        expected = hmac.new(
            auth_secret().encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(payload_b64))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("token expired")
        uid = payload.get("sub")
        if not uid:
            raise ValueError("no subject")
        return {"uid": uid, "email": payload.get("email", "")}
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"invalid token: {e}")
