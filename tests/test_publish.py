import json
from pathlib import Path

from bsc_radio.db import Database
from bsc_radio.publish import Publisher
from tests.helpers import settings


def test_empty_site_is_generated(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    result = Publisher(cfg, db).publish()
    assert result == {"episodes": 0}
    assert "Houblon FM" in (cfg.site_dir / "index.html").read_text()
    assert json.loads((cfg.site_dir / "data/search.json").read_text()) == []


def test_publish_removes_artifacts_for_hidden_episode(tmp_path: Path):
    cfg = settings(tmp_path)
    cfg.ensure_dirs()
    db = Database(cfg.database)
    db.migrate()
    episode_id = db.execute(
        """INSERT INTO episode(slug,title,format,station,status,script_json)
        VALUES(?,?,?,?,?,?)""",
        ("hidden", "Caché", "journal", "brasserie", "audio", "[]"),
    )
    source = cfg.data_dir / "audio" / "hidden.mp3"
    source.write_bytes(b"ID3")
    db.execute(
        "INSERT INTO audio(episode_id,path,duration_seconds,checksum) VALUES(?,?,?,?)",
        (episode_id, str(source), 1, "checksum"),
    )
    publisher = Publisher(cfg, db)
    publisher.publish()
    page = cfg.site_dir / "episodes" / "hidden.html"
    public_audio = cfg.site_dir / "audio" / "hidden.mp3"
    assert page.is_file() and public_audio.is_file()

    db.execute("UPDATE episode SET hidden=1 WHERE id=?", (episode_id,))
    publisher.publish()

    assert not page.exists()
    assert not public_audio.exists()


def test_nginx_returns_404_for_missing_episode_pages():
    config = Path("deploy/nginx.conf").read_text(encoding="utf-8")
    assert "location /episodes/ { try_files $uri =404; }" in config


def test_nginx_serves_only_published_static_audio_without_account_auth():
    config = Path("deploy/nginx.conf").read_text(encoding="utf-8")
    audio_location = config.split("location /audio/ {", 1)[1].split("}", 1)[0]
    assert "try_files $uri =404;" in audio_location
    assert "auth_request" not in audio_location
    assert "proxy_pass" not in audio_location


def test_live_player_retries_after_an_early_icecast_404():
    script = (Path(__file__).parents[1] / "site/public/assets/app.js").read_text(encoding="utf-8")
    assert "await audio.play()" in script
    assert "/live.mp3?attempt=${Date.now()}" in script
    assert "Antenne en cours de démarrage — réessayer" in script
    assert "play.textContent = 'Ⅱ'" in script
