from pathlib import Path

from fastapi.testclient import TestClient

from bsc_radio.db import Database
from bsc_radio.providers import MockVoiceProvider
from bsc_radio.sidecar import create_app
from tests.helpers import settings


def test_mock_voice_creates_mp3(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    output = tmp_path / "voice.mp3"
    MockVoiceProvider().synthesize("Un court essai de voix.", "mock", output)
    assert output.read_bytes()[:3] in {b"ID3", b"\xff\xfb", b"\xff\xf3"}


def test_health_and_auth_flow(tmp_path: Path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    assert client.get("/health").json()["ok"] is True
    fallback = cfg.data_dir / "fallback.mp3"
    assert fallback.is_file()
    assert fallback.stat().st_size > 0
    created = client.post(
        "/api/register",
        data={"email": "radio@example.test", "password": "a-secure-password"},
    )
    assert created.status_code == 200
    login = client.post(
        "/api/login",
        data={"email": "radio@example.test", "password": "a-secure-password"},
    )
    assert login.status_code == 200
    assert client.get("/api/me").json()["email"] == "radio@example.test"


def test_admin_requires_password(tmp_path: Path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    assert client.get("/api/admin/dashboard").status_code == 401
    assert (
        client.get("/api/admin/dashboard", headers={"x-radio-admin-password": "admin"}).status_code
        == 200
    )


def test_liquidsoap_has_an_infallible_last_resort():
    script = (Path(__file__).parents[1] / "deploy/liquidsoap.liq").read_text(encoding="utf-8")
    assert "mksafe(fallback(" in script
