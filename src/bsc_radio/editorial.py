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

FORMAT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "awards": (
        "award",
        "classement",
        "concours",
        "gagn",
        "laureat",
        "medaille",
        "palmares",
        "grand prix",
        "recompens",
        "trophee",
        "winner",
    ),
    "portrait": (
        "brewery",
        "fonde",
        "histoire",
        "microbrasserie",
        "portrait",
        "taproom",
        "visite",
    ),
    "recipe": (
        "brassage",
        "dry hop",
        "empatage",
        "fermentation",
        "houblon",
        "ingredient",
        "levure",
        "malt",
        "recette",
        "temperature",
    ),
    "style": (
        "ale",
        "amertume",
        "arome",
        "biere de style",
        "blonde",
        "brune",
        "gueuze",
        "ipa",
        "lager",
        "lambic",
        "neipa",
        "pils",
        "saison",
        "stout",
    ),
    "glossary": (
        "attenuation",
        "densite",
        "dry hop",
        "empatage",
        "fermentation",
        "ibu",
        "levure",
        "malt",
        "moût",
        "mout",
        "refermentation",
        "srm",
        "temperature",
    ),
}

FORMAT_ANCHORS: dict[str, tuple[str, ...]] = {
    "awards": FORMAT_KEYWORDS["awards"],
    "portrait": ("brewery", "fonde", "histoire", "portrait", "taproom", "visite"),
    "recipe": ("empatage", "ingredient", "recette"),
    "style": (
        "biere de style",
        "gueuze",
        "ipa",
        "lager",
        "lambic",
        "neipa",
        "pils",
        "saison",
        "stout",
    ),
    "glossary": FORMAT_KEYWORDS["glossary"],
}


def _searchable(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode().casefold()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:80] or "episode"


@dataclass
class EditorialPipeline:
    settings: Settings
    db: Database
    provider: EditorialProvider

    def _context(self) -> dict[str, Any]:
        radio = self.settings.radio
        return {
            "date": dt.datetime.now(dt.UTC).isoformat(),
            "disclosure": radio["ai_disclosure"],
            "health_notice": radio["health_notice"],
            "characters": self.settings.characters,
            "law": "Information objective. Aucune incitation, réussite, fête ou ciblage de mineurs.",
            "antenna": {
                "station": radio["name"],
                "tone": "radio vivante, chaleureuse, complice et légèrement impertinente",
                "dedication": "remercier ponctuellement les auditeurs et les métiers du brassage",
            },
        }

    def _persist_episode(
        self, fmt: str, script: list[dict[str, Any]], context: dict[str, Any], title: str | None
    ) -> int:
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
                """INSERT INTO sentence(
                episode_id,position,character,text,delivery_json,fact_ids_json,feedback_ids_json
                ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        episode_id,
                        pos,
                        line["character"],
                        line["text"],
                        json.dumps(line.get("delivery", [])),
                        json.dumps(line.get("fact_ids", [])),
                        json.dumps(line.get("feedback_ids", [])),
                    )
                    for pos, line in enumerate(script)
                ],
            )
        return int(episode_id)

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

    def facts_for_episode(
        self, hours: int = 48, limit: int = 30, fmt: str | None = None
    ) -> list[dict[str, Any]]:
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
        return [
            dict(row)
            for row in self.db.all(
                """WITH eligible AS (
                    SELECT DISTINCT f.*, i.source_id,
                        t.title AS topic_title, t.summary AS topic_summary,
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
                        AND (? IS NULL OR used_episode.format=?)
                    )
                )
                SELECT * FROM eligible
                ORDER BY source_rank, reliability DESC, source_id, created_at DESC
                LIMIT ?""",
                (threshold, fmt, fmt, limit),
            )
        ]

    def facts_for_format(self, fmt: str, hours: int, limit: int = 30) -> list[dict[str, Any]]:
        schedule = self.settings.schedule.get("formats", {}).get(fmt, {})
        effective_hours = max(hours, 720) if not schedule.get("fresh", True) else hours
        candidates = self.facts_for_episode(effective_hours, limit=150, fmt=fmt)
        keywords = FORMAT_KEYWORDS.get(fmt)
        if not keywords:
            return candidates[:limit]

        scored: list[tuple[int, dict[str, Any]]] = []
        for fact in candidates:
            haystack = _searchable(
                " ".join(
                    str(fact.get(field, ""))
                    for field in ("statement", "source_name", "topic_title", "topic_summary")
                )
            )
            score = sum(1 for keyword in keywords if _searchable(keyword) in haystack)
            anchors = FORMAT_ANCHORS.get(fmt, keywords)
            if score and any(_searchable(keyword) in haystack for keyword in anchors):
                scored.append((score, fact))
        if not scored:
            label = schedule.get("label", fmt)
            raise ValueError(
                f"Aucun fait adapté au format « {label} » sur cette période. "
                "Le format refuse de recycler une actualité sans rapport."
            )
        if fmt in {"portrait", "recipe", "style", "glossary"}:
            group_scores: dict[int, set[str]] = {}
            group_peak: dict[int, int] = {}
            for score, fact in scored:
                item_id = int(fact["item_id"])
                haystack = _searchable(
                    " ".join(
                        str(fact.get(field, ""))
                        for field in ("statement", "source_name", "topic_title", "topic_summary")
                    )
                )
                group_scores.setdefault(item_id, set()).update(
                    keyword for keyword in keywords if _searchable(keyword) in haystack
                )
                group_peak[item_id] = max(group_peak.get(item_id, 0), score)
            selected_item = max(
                group_scores,
                key=lambda item: (len(group_scores[item]), group_peak[item], -item),
            )
            return [fact for _, fact in scored if int(fact["item_id"]) == selected_item][:limit]
        return [fact for _, fact in sorted(scored, key=lambda pair: -pair[0])][:limit]

    def write_episode(self, fmt: str, hours: int = 48, title: str | None = None) -> int:
        facts = self.facts_for_format(fmt, hours)
        if not facts:
            raise ValueError(
                "Aucun fait inédit disponible pour cette période. "
                "Collectez de nouvelles sources ou augmentez --hours."
            )
        context = self._context()
        format_config = dict(self.settings.schedule.get("formats", {}).get(fmt, {}))
        min_minutes = float(format_config.get("min_minutes", 1))
        max_minutes = float(format_config.get("max_minutes", min_minutes + 1))
        allowed_roles = list(format_config.get("voices", ["presenter"]))
        context["format"] = {
            "id": fmt,
            "label": format_config.get("label", fmt.title()),
            "fresh": bool(format_config.get("fresh", True)),
            "allowed_characters": allowed_roles,
            "target_words": [round(min_minutes * 135), round(max_minutes * 135)],
        }
        script = canonicalize_script(
            self.provider.write(fmt, facts, context),
            self.settings.characters,
            allowed_roles=allowed_roles,
        )
        return self._persist_episode(fmt, script, context, title)

    def write_feedback_episode(self, title: str | None = None, limit: int = 20) -> int:
        rows = self.db.all(
            """SELECT id,rating,message,created_at FROM feedback
            WHERE status='pending' ORDER BY created_at,id LIMIT ?""",
            (limit,),
        )
        if not rows:
            raise ValueError("Aucun avis en attente pour le Courrier des auditeurs.")
        feedback_ids = {int(row["id"]) for row in rows}
        material = [
            {
                "id": int(row["id"]),
                "statement": row["message"],
                "type": "listener_feedback",
                "rating": row["rating"],
                "source_name": "Auditeur anonyme",
                "source_url": f"{self.settings.site_url}/avis.html",
                "source_date": row["created_at"],
                "reliability": 1.0,
            }
            for row in rows
        ]
        context = self._context()
        context["feedback_count"] = len(material)
        format_config = dict(self.settings.schedule.get("formats", {}).get("feedback", {}))
        allowed_roles = list(format_config.get("voices", ["audience", "presenter"]))
        context["format"] = {
            "id": "feedback",
            "label": format_config.get("label", "Le Courrier des auditeurs"),
            "fresh": False,
            "allowed_characters": allowed_roles,
            "target_words": [
                round(float(format_config.get("min_minutes", 1)) * 135),
                round(float(format_config.get("max_minutes", 2)) * 135),
            ],
        }
        raw_script = self.provider.write("feedback", material, context)
        for line in raw_script:
            referenced = line.get("feedback_ids") or line.get("fact_ids") or []
            valid_references: list[int] = []
            for value in referenced if isinstance(referenced, list) else []:
                try:
                    identifier = int(value)
                except (TypeError, ValueError):
                    continue
                if identifier in feedback_ids and identifier not in valid_references:
                    valid_references.append(identifier)
            line["feedback_ids"] = valid_references
            line["fact_ids"] = []
        script = canonicalize_script(
            raw_script, self.settings.characters, allowed_roles=allowed_roles
        )
        return self._persist_episode("feedback", script, context, title)

    def verify_episode(self, episode_id: int) -> dict[str, Any]:
        episode = self.db.one("SELECT format FROM episode WHERE id=?", (episode_id,))
        if not episode:
            raise ValueError(f"Épisode #{episode_id} introuvable")
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
            feedback_ids = json.loads(sentence["feedback_ids_json"] or "[]")
            if feedback_ids:
                placeholders = ",".join("?" for _ in feedback_ids)
                feedback_rows = self.db.all(
                    f"SELECT id,rating,message,created_at FROM feedback WHERE id IN ({placeholders})",
                    tuple(feedback_ids),
                )
                facts.extend(
                    {
                        "id": int(row["id"]),
                        "statement": row["message"],
                        "type": "listener_feedback",
                        "rating": row["rating"],
                        "source_name": "Auditeur anonyme",
                        "source_url": f"{self.settings.site_url}/avis.html",
                        "source_date": row["created_at"],
                        "reliability": 1.0,
                    }
                    for row in feedback_rows
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
                    """SELECT character,text,delivery_json,fact_ids_json,feedback_ids_json FROM sentence
                    WHERE episode_id=? AND verdict!='deleted' ORDER BY position""",
                    (episode_id,),
                )
            )
            script = [
                {
                    "character": row["character"],
                    "text": row["text"],
                    "delivery": json.loads(row["delivery_json"] or "[]"),
                    "fact_ids": json.loads(row["fact_ids_json"]),
                    "feedback_ids": json.loads(row["feedback_ids_json"] or "[]"),
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
            if status == "verified" and episode["format"] == "feedback":
                used_ids = {
                    feedback_id
                    for row in kept
                    for feedback_id in json.loads(row["feedback_ids_json"] or "[]")
                }
                if used_ids:
                    placeholders = ",".join("?" for _ in used_ids)
                    connection.execute(
                        f"UPDATE feedback SET status='used',episode_id=? WHERE id IN ({placeholders})",
                        (episode_id, *sorted(used_ids)),
                    )
        return {"status": status, "sentences": len(sentences), "rejected": rejected, "ratio": ratio}
