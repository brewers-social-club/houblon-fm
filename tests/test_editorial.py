import datetime as dt
import json
from pathlib import Path

import pytest

from bsc_radio.db import Database
from bsc_radio.editorial import EditorialPipeline, slugify
from bsc_radio.providers import MockEditorialProvider
from tests.helpers import settings


def add_fact(db: Database, source_id: str, statement: str) -> int:
    now = dt.datetime.now(dt.UTC).isoformat()
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (source_id, source_id.title(), "rss", f"https://{source_id}.test/feed", "brassage"),
    )
    item_id = db.execute(
        """INSERT INTO item(source_id,url,title,published_at,collected_at,content_hash,raw_text,status)
        VALUES(?,?,?,?,?,?,?,'facts')""",
        (source_id, f"https://{source_id}.test/article", source_id, now, now, source_id, statement),
    )
    topic_id = db.execute(
        """INSERT INTO topic(title,summary,station,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?)""",
        (source_id, statement, "brassage", now, now),
    )
    fact_id = db.execute(
        """INSERT INTO fact(item_id,statement,fact_type,reliability,source_url,source_name,source_date)
        VALUES(?,?,?,?,?,?,?)""",
        (item_id, statement, "context", 0.9, f"https://{source_id}.test/article", source_id, now),
    )
    db.execute("INSERT INTO topic_fact(topic_id,fact_id) VALUES(?,?)", (topic_id, fact_id))
    return fact_id


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


def test_feedback_episode_uses_pending_messages_and_marks_only_referenced_ones(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    first = db.execute(
        "INSERT INTO feedback(rating,message) VALUES(?,?)",
        (5, "J'aime les explications techniques et les dialogues."),
    )
    second = db.execute(
        "INSERT INTO feedback(message) VALUES(?)",
        ("Pourriez-vous parler davantage des levures ?",),
    )
    pipeline = EditorialPipeline(cfg, db, MockEditorialProvider())

    episode_id = pipeline.write_feedback_episode(title="Le courrier test")
    episode = db.one("SELECT format FROM episode WHERE id=?", (episode_id,))
    assert episode["format"] == "feedback"
    sentences = db.all(
        "SELECT fact_ids_json,feedback_ids_json FROM sentence WHERE episode_id=?", (episode_id,)
    )
    assert all(json.loads(row["fact_ids_json"]) == [] for row in sentences)
    referenced = {
        identifier for row in sentences for identifier in json.loads(row["feedback_ids_json"])
    }
    assert referenced == {first, second}

    result = pipeline.verify_episode(episode_id)
    assert result["status"] == "verified"
    used = db.all("SELECT id,status,episode_id FROM feedback ORDER BY id")
    assert [(row["id"], row["status"], row["episode_id"]) for row in used] == [
        (first, "used", episode_id),
        (second, "used", episode_id),
    ]


def test_feedback_episode_fails_fast_without_pending_message(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    pipeline = EditorialPipeline(cfg, db, MockEditorialProvider())

    with pytest.raises(ValueError, match="Aucun avis en attente"):
        pipeline.write_feedback_episode()


class FormatSpyProvider(MockEditorialProvider):
    def __init__(self):
        self.calls = []

    def write(self, fmt, facts, context):
        self.calls.append((fmt, facts, context))
        return [
            {
                "character": "audience",
                "text": f"Format {fmt}.",
                "delivery": [],
                "fact_ids": [facts[0]["id"]],
                "feedback_ids": [],
            }
        ]


def test_all_editorial_formats_have_prompts_filters_roles_and_duration_context(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    samples = {
        "awards": "Le concours a décerné une médaille et publié son palmarès.",
        "portrait": "Portrait d'une brasserie fondée dans une ancienne ferme.",
        "recipe": "La recette utilise du malt et une fermentation à vingt degrés.",
        "style": "Le style stout est une ale brune aux arômes torréfiés.",
        "glossary": "L'atténuation mesure le travail de la levure pendant la fermentation.",
        "news": "Une actualité générale du secteur brassicole a été publiée.",
    }
    for source_id, statement in samples.items():
        add_fact(db, source_id, statement)

    expected_role = {
        "journal": "presenter",
        "headlines": "presenter",
        "awards": "presenter",
        "week": "presenter",
        "portrait": "storyteller",
        "recipe": "consultant",
        "style": "storyteller",
        "glossary": "presenter",
    }
    provider = FormatSpyProvider()
    pipeline = EditorialPipeline(cfg, db, provider)
    for fmt, role in expected_role.items():
        episode_id = pipeline.write_episode(fmt, hours=48, title=f"Test {fmt}")
        sentence = db.one("SELECT character FROM sentence WHERE episode_id=?", (episode_id,))
        assert sentence["character"] == role
        _, facts, context = provider.calls[-1]
        assert context["format"]["allowed_characters"] == cfg.schedule["formats"][fmt]["voices"]
        assert context["format"]["target_words"][0] < context["format"]["target_words"][1]
        assert context["format"]["speech_words_per_minute"] == 150
        if fmt in samples:
            assert any(fmt in fact["source_id"] for fact in facts)
        assert (cfg.root / "prompts" / f"write_{fmt}.md").exists()


def test_common_prompt_requires_lively_humorous_delivery():
    prompt = Path("prompts/common.md").read_text(encoding="utf-8")
    assert "vrai fil humoristique" in prompt
    assert "une à trois répliques" in prompt
    assert "`brisk`" in prompt and "`deadpan`" in prompt


def test_week_can_recap_a_fact_already_used_by_journal(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    fact_id = add_fact(db, "weekly-news", "Une actualité de la semaine sur une brasserie.")
    pipeline = EditorialPipeline(cfg, db, MockEditorialProvider())

    pipeline.write_episode("journal")
    week_id = pipeline.write_episode("week", hours=168)
    refs = db.all("SELECT fact_ids_json FROM sentence WHERE episode_id=?", (week_id,))

    assert fact_id in {
        identifier for row in refs for identifier in json.loads(row["fact_ids_json"])
    }


def test_specialized_format_refuses_unrelated_news(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    add_fact(db, "finance", "Le chiffre d'affaires trimestriel a progressé.")
    pipeline = EditorialPipeline(cfg, db, MockEditorialProvider())

    with pytest.raises(ValueError, match="Aucun fait adapté"):
        pipeline.write_episode("recipe", hours=720)


def test_does_not_reuse_facts_already_reserved_by_an_episode(tmp_path: Path):
    _, _, pipeline = seeded(tmp_path)
    pipeline.extract_pending()
    pipeline.build_topics()
    pipeline.write_episode("journal")

    with pytest.raises(ValueError, match="Aucun fait inédit"):
        pipeline.write_episode("journal")


def test_metadata_only_youtube_facts_never_reach_an_episode(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    now = dt.datetime.now(dt.UTC).isoformat()
    db.execute(
        """INSERT INTO source(id,name,kind,url,station)
        VALUES('youtube','YouTube','youtube','https://youtube.test/channel','brassage')"""
    )
    item_id = db.execute(
        """INSERT INTO item(source_id,url,title,published_at,collected_at,content_hash,raw_text,
        status,metadata_json) VALUES(?,?,?,?,?,?,?,'facts',?)""",
        (
            "youtube",
            "https://www.youtube.com/watch?v=metadata",
            "Titre racoleur",
            now,
            now,
            "metadata",
            "Titre et description uniquement.",
            json.dumps({"platform": "youtube", "collection_mode": "public_feed"}),
        ),
    )
    topic_id = db.execute(
        """INSERT INTO topic(title,summary,station,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?)""",
        ("Sujet YouTube", "Résumé", "brassage", now, now),
    )
    fact_id = db.execute(
        """INSERT INTO fact(item_id,statement,fact_type,reliability,source_url,source_name)
        VALUES(?,?,?,?,?,?)""",
        (
            item_id,
            "La description promet une bière incroyable.",
            "context",
            0.9,
            "https://www.youtube.com/watch?v=metadata",
            "YouTube",
        ),
    )
    db.execute("INSERT INTO topic_fact(topic_id,fact_id) VALUES(?,?)", (topic_id, fact_id))

    facts = EditorialPipeline(cfg, db, MockEditorialProvider()).facts_for_episode()

    assert fact_id not in {fact["id"] for fact in facts}


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
            db.execute("INSERT INTO topic_fact(topic_id,fact_id) VALUES(?,?)", (topic_id, fact_id))

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
