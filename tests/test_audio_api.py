from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi.testclient import TestClient

from bsc_radio.audio import AudioProducer, duration, group_script_takes
from bsc_radio.db import Database
from bsc_radio.providers import ElevenLabsVoiceProvider, GeminiVoiceProvider, MockVoiceProvider
from bsc_radio.sidecar import create_app
from tests.helpers import settings


def test_mock_voice_creates_mp3(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    output = tmp_path / "voice.mp3"
    MockVoiceProvider().synthesize("Un court essai de voix.", "mock", output)
    assert output.read_bytes()[:3] in {b"ID3", b"\xff\xfb", b"\xff\xf3"}


def test_gemini_voice_wraps_raw_pcm_and_encodes_mp3(tmp_path: Path):
    inline_data = SimpleNamespace(
        data=b"\x00\x10" * 2400,
        mime_type="audio/L16;codec=pcm;rate=24000",
    )
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(inline_data=inline_data)])
            )
        ]
    )
    provider = object.__new__(GeminiVoiceProvider)
    provider.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_kwargs: response)
    )
    provider.model = "gemini-test-tts"
    output = tmp_path / "gemini.mp3"

    provider.synthesize("Une vraie voix.", "Kore", output)

    assert output.read_bytes()[:3] in {b"ID3", b"\xff\xfb", b"\xff\xf3"}
    assert duration(output) > 0


def test_elevenlabs_voice_requests_mp3_without_exposing_key(tmp_path: Path, monkeypatch):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "elevenlabs_api_key", "protected-test-key")
    calls = []

    class Response:
        content = b"ID3-real-voice"

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("bsc_radio.providers.httpx.post", fake_post)
    output = tmp_path / "elevenlabs.mp3"

    ElevenLabsVoiceProvider(cfg).synthesize("Une vraie voix.", "ignored", output)

    assert output.read_bytes() == b"ID3-real-voice"
    assert calls[0][0].endswith("/test-voice")
    assert calls[0][1]["params"] == {"output_format": "mp3_44100_128"}
    assert calls[0][1]["headers"]["xi-api-key"] == "protected-test-key"
    assert calls[0][1]["json"]["model_id"] == "test-model"
    assert calls[0][1]["json"]["voice_settings"]["style"] == 0.6


def test_consecutive_lines_share_one_voice_take():
    assert group_script_takes(
        [
            {"character": "presenter", "text": "Première phrase."},
            {"character": "presenter", "text": "Deuxième phrase."},
            {"character": "consultant", "text": "Réponse."},
        ]
    ) == [
        {"character": "presenter", "text": "Première phrase.\n\nDeuxième phrase."},
        {"character": "consultant", "text": "Réponse."},
    ]


def test_revoice_preserves_audio_id_referenced_by_airing(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    episode_id = db.execute(
        """INSERT INTO episode(slug,title,format,station,status,script_json)
        VALUES(?,?,?,?,?,?)""",
        (
            "episode-parle",
            "Épisode parlé",
            "journal",
            "brasserie",
            "verified",
            '[{"character":"presenter","text":"Bonjour la radio."}]',
        ),
    )
    audio_id = db.execute(
        """INSERT INTO audio(episode_id,path,duration_seconds,checksum)
        VALUES(?,?,?,?)""",
        (episode_id, "/ancien/audio.mp3", 1, "ancien"),
    )
    db.execute(
        "INSERT INTO airing(episode_id,audio_id,started_at) VALUES(?,?,CURRENT_TIMESTAMP)",
        (episode_id, audio_id),
    )

    output = AudioProducer(cfg, db, MockVoiceProvider()).produce(episode_id)

    row = db.one("SELECT id,path,checksum FROM audio WHERE episode_id=?", (episode_id,))
    assert row["id"] == audio_id
    assert row["path"] == str(output)
    assert row["checksum"] != "ancien"
    assert db.one("SELECT audio_id FROM airing WHERE episode_id=?", (episode_id,))["audio_id"] == audio_id


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


def test_next_maps_legacy_host_audio_path_into_shared_data_dir(tmp_path: Path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    audio = cfg.data_dir / "audio" / "episode.mp3"
    audio.write_bytes(b"ID3-test")
    database = Database(cfg.database)
    episode_id = database.execute(
        "INSERT INTO episode(slug,title,format,station,status) VALUES(?,?,?,?,?)",
        ("episode", "Épisode", "journal", "brasserie", "audio"),
    )
    database.execute(
        "INSERT INTO audio(episode_id,path,duration_seconds,checksum) VALUES(?,?,?,?)",
        (episode_id, "/ancienne/machine/data/audio/episode.mp3", 10, "test"),
    )

    response = client.get("/next")

    assert response.status_code == 200
    assert response.text == str(audio)


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
    assert "./data:/data" in compose["services"]["sidecar"]["volumes"]
    assert "./data:/data" in compose["services"]["liquidsoap"]["volumes"]
    assert "volumes" not in compose
