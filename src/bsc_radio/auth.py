from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets

from .db import Database


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Le mot de passe doit contenir au moins 10 caractères")
    salt = os.urandom(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return f"scrypt$16384$8$1${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(value).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt_bytes = base64.urlsafe_b64decode(salt)
        actual = hashlib.scrypt(
            password.encode(), salt=salt_bytes, n=int(n), r=int(r), p=int(p), dklen=64
        )
        return hmac.compare_digest(base64.urlsafe_b64decode(expected), actual)
    except (ValueError, TypeError):
        return False


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    def register(self, email: str, password: str) -> int:
        return self.db.execute(
            "INSERT INTO user(email,password_hash) VALUES(?,?)",
            (email.strip().lower(), hash_password(password)),
        )

    def login(self, email: str, password: str, days: int = 30) -> str | None:
        user = self.db.one("SELECT * FROM user WHERE email=? COLLATE NOCASE", (email.strip(),))
        if not user or not verify_password(password, user["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        expires = (dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).isoformat()
        self.db.execute(
            "INSERT INTO session(id,user_id,expires_at) VALUES(?,?,?)", (token, user["id"], expires)
        )
        return token

    def user_for_session(self, token: str | None):
        if not token:
            return None
        return self.db.one(
            """SELECT u.* FROM session s JOIN user u ON u.id=s.user_id
            WHERE s.id=? AND s.expires_at>?""",
            (token, dt.datetime.now(dt.UTC).isoformat()),
        )

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM session WHERE id=?", (token,))

    def one_time_token(self, user_id: int, kind: str, hours: int = 24) -> str:
        token = secrets.token_urlsafe(32)
        expires = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours)).isoformat()
        self.db.execute(
            "INSERT INTO auth_token(id,user_id,kind,expires_at) VALUES(?,?,?,?)",
            (token, user_id, kind, expires),
        )
        return token
