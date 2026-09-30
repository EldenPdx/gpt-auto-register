"""Single administrator session authentication."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from contextlib import closing

from . import db


COOKIE_NAME = "admin_session"
SESSION_AGE = 7 * 24 * 60 * 60
_PASSWORD_KEY = "admin_password_hash"


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300_000)
    return f"{salt.hex()}:{digest.hex()}"


def _password_hash() -> str:
    stored = db.get_setting(_PASSWORD_KEY)
    if stored:
        return stored
    with db._lock, closing(db._conn()) as con:
        con.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (_PASSWORD_KEY, _hash_password("xvanai666")),
        )
        con.commit()
    return db.get_setting(_PASSWORD_KEY)


def _matches_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 300_000)
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def verify_password(password: str) -> bool:
    return _matches_password(password, _password_hash())


def change_password(current_password: str, new_password: str) -> bool:
    stored = _password_hash()
    if not _matches_password(current_password, stored):
        return False
    with db._lock, closing(db._conn()) as con:
        updated = con.execute(
            "UPDATE settings SET value=? WHERE key=? AND value=?",
            (_hash_password(new_password), _PASSWORD_KEY, stored),
        ).rowcount
        if updated:
            con.execute("DELETE FROM admin_sessions")
        con.commit()
    return bool(updated)


def create_session() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    with db._lock, closing(db._conn()) as con:
        con.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (time.time(),))
        con.execute(
            "INSERT INTO admin_sessions(token_hash, csrf_token, expires_at) VALUES (?, ?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), csrf_token, time.time() + SESSION_AGE),
        )
        con.commit()
    return token, csrf_token


def get_session(token: str) -> dict | None:
    if not token or len(token) > 128:
        return None
    with closing(db._conn()) as con:
        row = con.execute(
            "SELECT csrf_token FROM admin_sessions WHERE token_hash=? AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), time.time()),
        ).fetchone()
    return {"csrf_token": row["csrf_token"]} if row else None


def revoke_session(token: str) -> None:
    with db._lock, closing(db._conn()) as con:
        con.execute(
            "DELETE FROM admin_sessions WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        con.commit()
