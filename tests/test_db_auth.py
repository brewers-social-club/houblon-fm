from pathlib import Path

from bsc_radio.auth import AuthService, hash_password, verify_password
from bsc_radio.db import Database
from tests.helpers import settings


def test_database_migrations_are_idempotent(tmp_path: Path):
    db = Database(settings(tmp_path).database)
    db.migrate()
    db.migrate()
    assert db.one("SELECT COUNT(*) count FROM schema_migration")["count"] == 3
    assert "delivery_json" in {
        row["name"] for row in db.all("PRAGMA table_info(sentence)")
    }
    with db.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_transaction_rolls_back(tmp_path: Path):
    db = Database(settings(tmp_path).database)
    db.migrate()
    try:
        with db.transaction() as connection:
            connection.execute("INSERT INTO kv(key,value) VALUES('x','1')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.one("SELECT * FROM kv WHERE key='x'") is None


def test_scrypt_password_roundtrip():
    encoded = hash_password("a-secure-password")
    assert verify_password("a-secure-password", encoded)
    assert not verify_password("wrong-password", encoded)


def test_session_login(tmp_path: Path):
    db = Database(settings(tmp_path).database)
    db.migrate()
    auth = AuthService(db)
    user_id = auth.register("test@example.com", "a-secure-password")
    token = auth.login("TEST@example.com", "a-secure-password")
    assert token and auth.user_for_session(token)["id"] == user_id
    auth.logout(token)
    assert auth.user_for_session(token) is None
