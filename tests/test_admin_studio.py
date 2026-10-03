from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi.testclient import TestClient

from bsc_radio.db import Database
from bsc_radio.sidecar import create_app
from tests.helpers import settings


def _seed(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    now = dt.datetime.now(dt.UTC).isoformat()
    db.execute(
        """INSERT INTO source(id,name,kind,url,station,enabled)
        VALUES('source-test','Source test','rss','https://example.test/feed','brasserie',1)"""
    )
    db.execute(
        """INSERT INTO source_fetch(source_id,started_at,finished_at,item_count)
        VALUES('source-test',?,?,1)""",
        (now, now),
    )
    item_id = db.execute(
        """INSERT INTO item(source_id,url,title,published_at,collected_at,content_hash,raw_text,status)
        VALUES('source-test','https://example.test/article','Un article',?,?,?,'Texte complet','processed')""",
        (now, now, "hash-test"),
    )
    fact_id = db.execute(
        """INSERT INTO fact(item_id,statement,fact_type,reliability,source_url,source_name,source_date)
        VALUES(?,?,?,?,?,?,?)""",
        (
            item_id,
            "Un fait sourcé.",
            "context",
            0.9,
            "https://example.test/article",
            "Source test",
            now,
        ),
    )
    topic_id = db.execute(
        """INSERT INTO topic(title,summary,station,status,first_seen_at,last_seen_at)
        VALUES('Sujet test','Résumé du sujet','brasserie','open',?,?)""",
        (now, now),
    )
    db.execute("INSERT INTO topic_fact(topic_id,fact_id) VALUES(?,?)", (topic_id, fact_id))
    episode_id = db.execute(
        """INSERT INTO episode(slug,title,format,station,status,summary,script_json)
        VALUES('episode-test','Épisode test','journal','brasserie','published','Résumé',?)""",
        (json.dumps([{"character": "presenter", "text": "Bonjour.", "fact_ids": [fact_id]}]),),
    )
    db.execute(
        """INSERT INTO sentence(episode_id,position,character,text,fact_ids_json,verdict)
        VALUES(?,0,'presenter','Bonjour.',?,'supported')""",
        (episode_id, json.dumps([fact_id])),
    )
    audio_path = cfg.data_dir / "audio" / "episode-test.mp3"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"ID3-test")
    audio_id = db.execute(
        """INSERT INTO audio(episode_id,path,duration_seconds,bitrate,lufs,checksum)
        VALUES(?,?,30,128,-16,'checksum')""",
        (episode_id, str(audio_path)),
    )
    db.execute(
        "INSERT INTO airing(episode_id,audio_id,started_at,reason) VALUES(?,?,?,'test')",
        (episode_id, audio_id, now),
    )
    db.execute(
        "INSERT INTO pipeline_run(command,status,finished_at) VALUES('run','succeeded',?)",
        (now,),
    )
    return cfg, db, episode_id, item_id, topic_id


def _client(tmp_path: Path):
    cfg, db, episode_id, item_id, topic_id = _seed(tmp_path)
    client = TestClient(create_app(cfg))
    login = client.post("/api/admin/login", data={"login": "admin", "password": "admin"})
    assert login.status_code == 200
    return client, db, episode_id, item_id, topic_id


def test_admin_studio_exposes_all_read_models(tmp_path: Path):
    client, _db, episode_id, item_id, topic_id = _client(tmp_path)
    paths = [
        "/api/admin/overview",
        "/api/admin/episodes",
        f"/api/admin/episodes/{episode_id}",
        "/api/admin/sources",
        "/api/admin/sources/source-test/fetches",
        "/api/admin/items",
        f"/api/admin/items/{item_id}",
        "/api/admin/topics",
        f"/api/admin/topics/{topic_id}",
        "/api/admin/facts",
        "/api/admin/antenna",
        "/api/admin/operations",
        "/api/admin/logs",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)

    assert client.get("/api/admin/episodes").json()["pagination"]["total"] == 1
    detail = client.get(f"/api/admin/episodes/{episode_id}").json()
    assert detail["sentences"][0]["facts"][0]["statement"] == "Un fait sourcé."
    assert client.get("/api/admin/sources").json()[0]["item_count"] == 1


def test_admin_studio_requires_authentication(tmp_path: Path):
    cfg, *_ = _seed(tmp_path)
    client = TestClient(create_app(cfg))
    for path in (
        "/api/admin/overview",
        "/api/admin/episodes",
        "/api/admin/sources",
        "/api/admin/items",
        "/api/admin/topics",
        "/api/admin/facts",
        "/api/admin/antenna",
        "/api/admin/operations",
        "/api/admin/logs",
    ):
        assert client.get(path).status_code == 401


def test_admin_can_toggle_episode_visibility(tmp_path: Path):
    client, db, episode_id, *_ = _client(tmp_path)
    hidden = client.post(
        f"/api/admin/episodes/{episode_id}/visibility",
        data={"hidden": "true"},
    )
    assert hidden.status_code == 200
    assert db.one("SELECT hidden FROM episode WHERE id=?", (episode_id,))["hidden"] == 1
    visible = client.post(
        f"/api/admin/episodes/{episode_id}/visibility",
        data={"hidden": "false"},
    )
    assert visible.status_code == 200
    assert db.one("SELECT hidden FROM episode WHERE id=?", (episode_id,))["hidden"] == 0


def test_admin_ui_contains_studio_sections():
    root = Path(__file__).parents[1]
    html = (root / "site/public/admin/index.html").read_text(encoding="utf-8")
    script = (root / "site/public/admin/app.js").read_text(encoding="utf-8")
    css = (root / "site/public/admin/admin.css").read_text(encoding="utf-8")
    for label in ("Épisodes", "Collecte", "Éditorial", "Antenne", "Exploitation"):
        assert label in html
    assert "/episodes" in script
    assert "/sources" in script
    assert "/antenna" in script
    assert "@media(max-width:680px)" in css
