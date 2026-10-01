import datetime as dt
import json
from pathlib import Path

from bsc_radio.db import Database
from bsc_radio.editorial import EditorialPipeline, slugify
from bsc_radio.providers import MockEditorialProvider
from tests.helpers import settings


def seeded(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES('s','Source','rss','https://x.test/feed','brassage')"
    )
    now = dt.datetime.now(dt.UTC).isoformat()
    db.execute(
        "INSERT INTO item(source_id,url,title,published_at,collected_at,content_hash,raw_text) VALUES(?,?,?,?,?,?,?)",
        (
            "s",
            "https://x.test/a",
            "Titre",
            now,
            now,
            "hash",
            "La fermentation reste stable pendant cinq jours. Le résultat dépend de cette recette et de cette levure.",
        ),
    )
    return cfg, db, EditorialPipeline(cfg, db, MockEditorialProvider())


def test_extract_topic_write_verify(tmp_path: Path):
    _, db, pipeline = seeded(tmp_path)
    assert pipeline.extract_pending() == 1
    assert db.one("SELECT COUNT(*) count FROM fact")["count"] >= 1
    assert pipeline.build_topics() == 1
    episode = pipeline.write_episode("journal")
    result = pipeline.verify_episode(episode)
    assert result["status"] == "verified"
    assert json.loads(
        db.one("SELECT script_json FROM episode WHERE id=?", (episode,))["script_json"]
    )


def test_slugify_accents():
    assert slugify("L'Été de la Bière !") == "l-ete-de-la-biere"


class RejectingProvider(MockEditorialProvider):
    def verify(self, sentence, facts):
        return {"verdict": "unsupported", "flags": [], "note": "no"}

    def rewrite(self, sentence, facts):
        return ""


def test_rejects_episode_over_twenty_percent(tmp_path: Path):
    cfg, db, _ = seeded(tmp_path)
    pipeline = EditorialPipeline(cfg, db, RejectingProvider())
    pipeline.extract_pending()
    pipeline.build_topics()
    episode = pipeline.write_episode("journal")
    assert pipeline.verify_episode(episode)["status"] == "rejected"
