from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .characters import canonicalize_script
from .config import Settings
from .db import Database
from .providers import EditorialProvider


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:80] or "episode"


@dataclass
class EditorialPipeline:
    settings: Settings
    db: Database
    provider: EditorialProvider

    def extract_pending(self, limit: int = 25) -> int:
        items = self.db.all(
            """SELECT i.*, s.name source_name FROM item i JOIN source s ON s.id=i.source_id
            WHERE i.status='new' AND i.raw_text IS NOT NULL ORDER BY i.published_at DESC LIMIT ?""",
            (limit,),
        )
        done = 0
        for item in items:
            source = {"name": item["source_name"], "url": item["url"], "date": item["published_at"]}
            facts = self.provider.extract_facts(item["title"], item["raw_text"], source)
            with self.db.transaction() as connection:
                for fact in facts:
                    connection.execute(
                        """INSERT INTO fact(item_id,statement,fact_type,reliability,quote,source_url,source_name,source_date)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            item["id"],
                            fact["statement"],
                            fact.get("type", "context"),
                            float(fact.get("reliability", 0.5)),
                            fact.get("quote"),
                            item["url"],
                            item["source_name"],
                            item["published_at"],
                        ),
                    )
                connection.execute("UPDATE item SET status='facts' WHERE id=?", (item["id"],))
            done += 1
        return done

    def build_topics(self, hours: int = 72) -> int:
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
        orphan_facts = self.db.all(
            """SELECT f.* FROM fact f LEFT JOIN topic_fact tf ON tf.fact_id=f.id
            WHERE tf.fact_id IS NULL AND f.created_at >= ? ORDER BY f.created_at""",
            (threshold,),
        )
        if not orphan_facts:
            return 0
        # Premier regroupement volontairement conservateur : par source/item.
        groups: dict[int, list[dict[str, Any]]] = {}
        for fact in orphan_facts:
            groups.setdefault(fact["item_id"], []).append(dict(fact))
        for facts in groups.values():
            topic = self.provider.cluster(facts)
            now = dt.datetime.now(dt.UTC).isoformat()
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    """INSERT INTO topic(title,summary,station,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,?)""",
                    (topic["title"], topic["summary"], topic.get("station", "brasserie"), now, now),
                )
                topic_id = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO topic_fact(topic_id,fact_id) VALUES(?,?)",
                    [(topic_id, fact["id"]) for fact in facts],
                )
        return len(groups)

    def facts_for_episode(self, hours: int = 48, limit: int = 30) -> list[dict[str, Any]]:
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
        return [
            dict(row)
            for row in self.db.all(
                """WITH eligible AS (
                    SELECT DISTINCT f.*, i.source_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY i.source_id
                            ORDER BY f.reliability DESC, f.created_at DESC
                        ) AS source_rank
                    FROM fact f
                    JOIN item i ON i.id=f.item_id
                    JOIN topic_fact tf ON tf.fact_id=f.id
                    JOIN topic t ON t.id=tf.topic_id
                    WHERE i.published_at >= ?
                    AND NOT EXISTS (
                        SELECT 1
                        FROM sentence used_sentence
                        JOIN episode used_episode ON used_episode.id=used_sentence.episode_id
                        JOIN json_each(used_sentence.fact_ids_json) used_fact
                        WHERE CAST(used_fact.value AS INTEGER)=f.id
                        AND used_episode.status!='rejected'
                    )
                )
                SELECT * FROM eligible
                ORDER BY source_rank, reliability DESC, created_at DESC
                LIMIT ?""",
                (threshold, limit),
            )
        ]

    def write_episode(self, fmt: str, hours: int = 48, title: str | None = None) -> int:
        facts = self.facts_for_episode(hours)
        if not facts:
            raise ValueError(
                "Aucun fait inédit disponible pour cette période. "
                "Collectez de nouvelles sources ou augmentez --hours."
            )
        radio = self.settings.radio
        context = {
            "date": dt.datetime.now(dt.UTC).isoformat(),
            "disclosure": radio["ai_disclosure"],
            "health_notice": radio["health_notice"],
            "characters": self.settings.characters,
            "law": "Information objective. Aucune incitation, réussite, fête ou ciblage de mineurs.",
        }
        script = canonicalize_script(
            self.provider.write(fmt, facts, context), self.settings.characters
        )
        episode_title = title or self.settings.schedule["formats"].get(fmt, {}).get(
            "label", fmt.title()
        )
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        slug = f"{slugify(episode_title)}-{timestamp}"
        prompt_material = json.dumps(context, sort_keys=True)
        for path in sorted((self.settings.root / "prompts").glob("*.md")):
            prompt_material += path.name + path.read_text(encoding="utf-8")
        prompt_hash = hashlib.sha256(prompt_material.encode()).hexdigest()
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO episode(slug,title,format,station,status,summary,script_json,prompt_hash)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    slug,
                    episode_title,
                    fmt,
                    "brasserie",
                    "draft",
                    "Une émission brassicole sourcée et vérifiée.",
                    json.dumps(script, ensure_ascii=False),
                    prompt_hash,
                ),
            )
            episode_id = cursor.lastrowid
            connection.executemany(
                """INSERT INTO sentence(episode_id,position,character,text,fact_ids_json)
                VALUES(?,?,?,?,?)""",
                [
                    (
                        episode_id,
                        pos,
                        line["character"],
                        line["text"],
                        json.dumps(line.get("fact_ids", [])),
                    )
                    for pos, line in enumerate(script)
                ],
            )
        return int(episode_id)

    def verify_episode(self, episode_id: int) -> dict[str, Any]:
        sentences = self.db.all(
            "SELECT * FROM sentence WHERE episode_id=? ORDER BY position", (episode_id,)
        )
        rejected = 0
        for sentence in sentences:
            ids = json.loads(sentence["fact_ids_json"])
            facts = (
                [
                    dict(row)
                    for row in self.db.all(
                        f"SELECT * FROM fact WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids)
                    )
                ]
                if ids
                else []
            )
            result = self.provider.verify(sentence["text"], facts)
            verdict = result.get("verdict", "unsupported")
            text = sentence["text"]
            if verdict not in {"supported", "partial"}:
                rewritten = self.provider.rewrite(text, facts)
                second = (
                    self.provider.verify(rewritten, facts)
                    if rewritten
                    else {"verdict": "unsupported"}
                )
                if second.get("verdict") in {"supported", "partial"}:
                    text, verdict = rewritten, second["verdict"]
                else:
                    verdict = "deleted"
                    rejected += 1
            self.db.execute(
                "UPDATE sentence SET text=?,verdict=?,verifier_note=? WHERE id=?",
                (text, verdict, result.get("note"), sentence["id"]),
            )
        ratio = rejected / max(1, len(sentences))
        status = "rejected" if ratio > 0.20 else "verified"
        with self.db.transaction() as connection:
            kept = list(
                connection.execute(
                    "SELECT character,text,fact_ids_json FROM sentence WHERE episode_id=? AND verdict!='deleted' ORDER BY position",
                    (episode_id,),
                )
            )
            script = [
                {
                    "character": row["character"],
                    "text": row["text"],
                    "fact_ids": json.loads(row["fact_ids_json"]),
                }
                for row in kept
            ]
            connection.execute(
                "UPDATE episode SET status=?,script_json=?,rejection_reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    status,
                    json.dumps(script, ensure_ascii=False),
                    f"{ratio:.0%} supprimées" if status == "rejected" else None,
                    episode_id,
                ),
            )
        return {"status": status, "sentences": len(sentences), "rejected": rejected, "ratio": ratio}
