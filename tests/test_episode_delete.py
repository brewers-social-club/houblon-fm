from pathlib import Path

import pytest

from bsc_radio.db import Database
from bsc_radio.episodes import delete_episode
from bsc_radio.publish import Publisher
from tests.helpers import settings


def _published_episode(tmp_path: Path):
    cfg = settings(tmp_path)
    cfg.ensure_dirs()
    db = Database(cfg.database)
    db.migrate()
    episode_id = db.execute(
        """INSERT INTO episode(slug,title,format,station,status,summary,script_json)
        VALUES(?,?,?,?,?,?,?)""",
        (
            "episode-a-supprimer",
            "Épisode à supprimer",
            "journal",
            "brasserie",
            "audio",
            "Résumé",
            '[{"character":"presenter","text":"Texte."}]',
        ),
    )
    db.execute(
        """INSERT INTO sentence(episode_id,position,character,text,fact_ids_json,verdict)
        VALUES(?,?,?,?,?,?)""",
        (episode_id, 0, "presenter", "Texte.", "[]", "supported"),
    )
    audio_path = cfg.data_dir / "audio" / "episode-a-supprimer.mp3"
    audio_path.write_bytes(b"ID3-audio")
    audio_id = db.execute(
        """INSERT INTO audio(episode_id,path,duration_seconds,checksum)
        VALUES(?,?,?,?)""",
        (episode_id, str(audio_path), 10, "checksum"),
    )
    db.execute(
        "INSERT INTO airing(episode_id,audio_id,started_at) VALUES(?,?,CURRENT_TIMESTAMP)",
        (episode_id, audio_id),
    )
    db.execute(
        "INSERT INTO feedback(episode_id,message) VALUES(?,?)",
        (episode_id, "Avis"),
    )
    Publisher(cfg, db).publish()
    return cfg, db, episode_id, audio_path


def test_delete_episode_removes_database_audio_and_web_artifacts(tmp_path: Path):
    cfg, db, episode_id, audio_path = _published_episode(tmp_path)
    page = cfg.site_dir / "episodes" / "episode-a-supprimer.html"
    published_audio = cfg.site_dir / "audio" / audio_path.name
    assert page.is_file()
    assert published_audio.is_file()

    result = delete_episode(cfg, db, episode_id)

    assert result["audio_files"] == 1
    assert db.one("SELECT id FROM episode WHERE id=?", (episode_id,)) is None
    assert db.one("SELECT id FROM sentence WHERE episode_id=?", (episode_id,)) is None
    assert db.one("SELECT id FROM audio WHERE episode_id=?", (episode_id,)) is None
    assert db.one("SELECT id FROM airing WHERE episode_id=?", (episode_id,)) is None
    assert db.one("SELECT id FROM feedback WHERE episode_id=?", (episode_id,)) is None
    assert not audio_path.exists()
    assert not page.exists()
    assert not published_audio.exists()
    assert "Épisode à supprimer" not in (cfg.site_dir / "index.html").read_text(encoding="utf-8")


def test_delete_episode_refuses_audio_outside_managed_directory(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    episode_id = db.execute(
        "INSERT INTO episode(slug,title,format,station,status) VALUES(?,?,?,?,?)",
        ("unsafe", "Unsafe", "journal", "brasserie", "audio"),
    )
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"do-not-delete")
    db.execute(
        "INSERT INTO audio(episode_id,path,duration_seconds,checksum) VALUES(?,?,?,?)",
        (episode_id, str(outside), 1, "checksum"),
    )

    with pytest.raises(ValueError, match="hors du répertoire géré"):
        delete_episode(cfg, db, episode_id)

    assert outside.is_file()
    assert db.one("SELECT id FROM episode WHERE id=?", (episode_id,)) is not None


def test_delete_episode_refuses_shared_audio_path(tmp_path: Path):
    cfg, db, episode_id, audio_path = _published_episode(tmp_path)
    other_id = db.execute(
        "INSERT INTO episode(slug,title,format,station,status) VALUES(?,?,?,?,?)",
        ("autre", "Autre", "journal", "brasserie", "audio"),
    )
    db.execute(
        "INSERT INTO audio(episode_id,path,duration_seconds,checksum) VALUES(?,?,?,?)",
        (other_id, str(audio_path), 1, "shared"),
    )

    with pytest.raises(ValueError, match="partagé"):
        delete_episode(cfg, db, episode_id)

    assert db.one("SELECT id FROM episode WHERE id=?", (episode_id,)) is not None
    assert audio_path.exists()
