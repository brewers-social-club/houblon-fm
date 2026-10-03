import json
from pathlib import Path

from typer.testing import CliRunner

from bsc_radio import cli
from bsc_radio.audio import AudioProducer
from bsc_radio.db import Database
from bsc_radio.editorial import EditorialPipeline
from bsc_radio.providers import MockEditorialProvider, MockVoiceProvider
from tests.helpers import settings

runner = CliRunner()


def draft_services(tmp_path: Path):
    cfg = settings(tmp_path)
    cfg.ensure_dirs()
    db = Database(cfg.database)
    db.migrate()
    script = [{"character": "presenter", "text": "Bonjour la radio.", "fact_ids": []}]
    episode_id = db.execute(
        """INSERT INTO episode(slug,title,format,station,status,script_json)
        VALUES(?,?,?,?,?,?)""",
        (
            "journal-incomplet",
            "Journal incomplet",
            "journal",
            "brasserie",
            "draft",
            json.dumps(script),
        ),
    )
    db.execute(
        """INSERT INTO sentence(episode_id,position,character,text,fact_ids_json)
        VALUES(?,?,?,?,?)""",
        (episode_id, 0, "presenter", "Bonjour la radio.", "[]"),
    )
    editorial = EditorialPipeline(cfg, db, MockEditorialProvider())
    audio = AudioProducer(cfg, db, MockVoiceProvider())
    return cfg, db, editorial, audio, episode_id


def test_commands_are_visible_in_help():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "episodes" in result.stdout
    assert "resume" in result.stdout
    assert "voices" in result.stdout
    assert "logs" in result.stdout
    assert "runs" in result.stdout
    assert "delete" in result.stdout
    assert "imaging" in result.stdout
    assert "antenna-status" in result.stdout


def test_run_rejects_unknown_format_before_creating_a_pipeline_run(tmp_path: Path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    monkeypatch.setattr(cli.Settings, "load", lambda _root=None: cfg)

    result = runner.invoke(cli.app, ["run", "--fmt", "samedi-03-10-2026"])

    assert result.exit_code == 2
    assert "format inconnu" in result.output
    assert "journal" in result.output
    assert db.one("SELECT COUNT(*) count FROM pipeline_run")["count"] == 0


def test_say_resolves_display_name_and_provider_voice(tmp_path: Path, monkeypatch):
    cfg, db, editorial, audio, _ = draft_services(tmp_path)
    object.__setattr__(cfg, "tts_provider", "elevenlabs")
    calls = []

    class VoiceSpy:
        @staticmethod
        def synthesize(text, voice, output, style=""):
            calls.append((text, voice, output, style))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"ID3")

    monkeypatch.setattr(cli, "services", lambda: (cfg, db, editorial, audio))
    monkeypatch.setattr(cli, "voice_provider", lambda _settings: VoiceSpy())

    result = runner.invoke(cli.app, ["say", "Salut Marcel.", "--character", "Marcel"])

    assert result.exit_code == 0
    assert calls[0][1] == "hqfrgApggtO1785R4Fsn"
    assert calls[0][2].name == "consultant.mp3"


def test_delete_requires_confirmation_and_then_removes_episode(tmp_path: Path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    episode_id = db.execute(
        "INSERT INTO episode(slug,title,format,station,status) VALUES(?,?,?,?,?)",
        ("delete-me", "À supprimer", "journal", "brasserie", "draft"),
    )
    monkeypatch.setattr(cli.Settings, "load", lambda _root=None: cfg)

    refused = runner.invoke(cli.app, ["delete", str(episode_id)])
    assert refused.exit_code == 2
    assert "--confirm" in refused.output
    assert db.one("SELECT id FROM episode WHERE id=?", (episode_id,)) is not None

    confirmed = runner.invoke(cli.app, ["delete", str(episode_id), "--confirm"])
    assert confirmed.exit_code == 0
    assert "supprimé définitivement" in confirmed.output
    assert db.one("SELECT id FROM episode WHERE id=?", (episode_id,)) is None


def test_episodes_lists_drafts_and_resume_publishes_them(tmp_path: Path, monkeypatch):
    cfg, db, editorial, audio, episode_id = draft_services(tmp_path)
    monkeypatch.setattr(cli, "services", lambda: (cfg, db, editorial, audio))

    listing = runner.invoke(cli.app, ["episodes", "--status", "draft"])
    assert listing.exit_code == 0
    assert f"#{episode_id} [draft]" in listing.stdout

    resumed = runner.invoke(cli.app, ["resume", str(episode_id)])
    assert resumed.exit_code == 0
    assert f"Épisode #{episode_id} publié" in resumed.stdout
    assert db.one("SELECT status FROM episode WHERE id=?", (episode_id,))["status"] == "published"
    assert (
        db.one("SELECT COUNT(*) count FROM audio WHERE episode_id=?", (episode_id,))["count"] == 1
    )

    second_resume = runner.invoke(cli.app, ["resume", str(episode_id)])
    assert second_resume.exit_code == 0
    assert "déjà publié" in second_resume.stdout


def test_resume_records_failed_stage_and_incident(tmp_path: Path, monkeypatch):
    cfg, db, _, audio, episode_id = draft_services(tmp_path)

    class BrokenEditorial:
        @staticmethod
        def verify_episode(_episode_id):
            raise RuntimeError("vérificateur indisponible")

    monkeypatch.setattr(cli, "services", lambda: (cfg, db, BrokenEditorial(), audio))

    result = runner.invoke(cli.app, ["resume", str(episode_id)])

    assert result.exit_code == 1
    episode = db.one("SELECT status,rejection_reason FROM episode WHERE id=?", (episode_id,))
    assert episode["status"] == "failed"
    assert episode["rejection_reason"].startswith("verify:")
    incident = db.one("SELECT kind,context_json FROM incident ORDER BY id DESC LIMIT 1")
    assert incident["kind"] == "pipeline"
    assert json.loads(incident["context_json"]) == {
        "episode_id": episode_id,
        "stage": "verify",
    }
