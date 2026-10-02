from pathlib import Path

import yaml
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


def test_next_returns_generated_fallback_when_library_is_empty(tmp_path: Path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    response = client.get("/next")
    assert response.status_code == 200
    assert response.text == str(cfg.data_dir / "fallback.mp3")
    assert Path(response.text).is_file()


def test_admin_requires_password(tmp_path: Path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    assert client.get("/api/admin/dashboard").status_code == 401
    assert (
        client.get("/api/admin/dashboard", headers={"x-radio-admin-password": "admin"}).status_code
        == 200
    )


def test_liquidsoap_has_an_infallible_last_resort():
    root = Path(__file__).parents[1]
    script = (root / "deploy/liquidsoap.liq").read_text(encoding="utf-8")
    dockerfile = (root / "deploy/Dockerfile.liquidsoap").read_text(encoding="utf-8")
    assert "mksafe(fallback(" in script
    assert 'process.read("/usr/bin/curl -fsS --max-time 5 ' in script
    assert "single(fallible=true, fallback_file)" in script
    assert "FROM savonet/liquidsoap:v2.3.2" in dockerfile
    assert "apt-get install -y --no-install-recommends curl" in dockerfile


def test_compose_forces_shared_container_paths():
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["sidecar"]["environment"]
    assert environment == {
        "RADIO_DATA_DIR": "/data",
        "RADIO_CONFIG_DIR": "/app/config",
        "RADIO_SITE_DIR": "/site",
    }
    assert compose["services"]["liquidsoap"]["build"] == {
        "context": ".",
        "dockerfile": "deploy/Dockerfile.liquidsoap",
    }
