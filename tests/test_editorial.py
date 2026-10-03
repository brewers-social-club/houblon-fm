import datetime as dt
import json
from pathlib import Path

import pytest

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
    script = json.loads(
        db.one("SELECT script_json FROM episode WHERE id=?", (episode,))["script_json"]
    )
    assert script
    assert all("delivery" in line for line in script)
    assert any(line["delivery"] == ["warm"] for line in script)
    stored = db.all(
        "SELECT delivery_json FROM sentence WHERE episode_id=? ORDER BY position", (episode,)
    )
    assert any(json.loads(row["delivery_json"]) == ["warm"] for row in stored)


def test_does_not_reuse_facts_already_reserved_by_an_episode(tmp_path: Path):
    _, _, pipeline = seeded(tmp_path)
    pipeline.extract_pending()
    pipeline.build_topics()
    pipeline.write_episode("journal")

    with pytest.raises(ValueError, match="Aucun fait inédit"):
        pipeline.write_episode("journal")


def test_balances_new_facts_between_sources(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    now = dt.datetime.now(dt.UTC).isoformat()
    for source_id in ("alpha", "beta"):
        db.execute(
            "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
            (source_id, source_id.title(), "rss", f"https://{source_id}.test/feed", "brassage"),
        )
        item_id = db.execute(
            """INSERT INTO item(source_id,url,title,published_at,collected_at,content_hash,raw_text,status)
            VALUES(?,?,?,?,?,?,?,'facts')""",
            (
                source_id,
                f"https://{source_id}.test/article",
                source_id.title(),
                now,
                now,
                source_id,
                "texte",
            ),
        )
        topic_id = db.execute(
            """INSERT INTO topic(title,summary,station,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?)""",
            (source_id.title(), "Résumé", "brassage", now, now),
        )
        for index in range(2):
            fact_id = db.execute(
                """INSERT INTO fact(item_id,statement,fact_type,reliability,source_url,source_name,source_date)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    item_id,
                    f"Fait {source_id} {index}",
                    "context",
                    0.9 - index / 10,
                    f"https://{source_id}.test/article",
                    source_id.title(),
                    now,
                ),
            )
            db.execute(
                "INSERT INTO topic_fact(topic_id,fact_id) VALUES(?,?)", (topic_id, fact_id)
            )

    pipeline = EditorialPipeline(cfg, db, MockEditorialProvider())
    facts = pipeline.facts_for_episode(limit=4)

    assert [fact["source_id"] for fact in facts[:2]] == ["alpha", "beta"]


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
