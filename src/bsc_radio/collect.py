from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.robotparser
from collections import Counter
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .observability import PipelineLog, redact, sanitize_url


def _date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.astimezone(dt.UTC)
    except (TypeError, ValueError, OverflowError):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)
        except ValueError:
            return None


def _entry_date(entry: dict[str, Any]) -> dt.datetime | None:
    parsed = _date(entry.get("published") or entry.get("updated"))
    if parsed:
        return parsed
    structured = entry.get("published_parsed") or entry.get("updated_parsed")
    if structured:
        try:
            return dt.datetime(*structured[:6], tzinfo=dt.UTC)
        except (TypeError, ValueError):
            return None
    return None


def _entry_text(entry: dict[str, Any]) -> str:
    fragments = [
        item.get("value", "") for item in entry.get("content", []) if isinstance(item, dict)
    ]
    fragments.extend([entry.get("summary", ""), entry.get("description", "")])
    plain = " ".join(fragment for fragment in fragments if fragment)
    plain = re.sub(r"<[^>]+>", " ", plain)
    return re.sub(r"\s+", " ", html.unescape(plain)).strip()


def _entry_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    audio = next(
        (
            enclosure
            for enclosure in entry.get("enclosures", [])
            if isinstance(enclosure, dict) and str(enclosure.get("type", "")).startswith("audio/")
        ),
        {},
    )
    image = entry.get("image") if isinstance(entry.get("image"), dict) else {}
    return {
        "guid": entry.get("id"),
        "audio_url": sanitize_url(str(audio.get("href", ""))) if audio.get("href") else None,
        "audio_type": audio.get("type"),
        "audio_bytes": int(audio["length"]) if str(audio.get("length", "")).isdigit() else None,
        "duration": entry.get("itunes_duration"),
        "season": entry.get("itunes_season") or entry.get("podcast_season"),
        "episode": entry.get("itunes_episode") or entry.get("podcast_episode"),
        "episode_type": entry.get("itunes_episodetype"),
        "explicit": entry.get("itunes_explicit"),
        "image_url": sanitize_url(str(image.get("href", ""))) if image.get("href") else None,
        "tags": [
            str(tag.get("term"))
            for tag in entry.get("tags", [])
            if isinstance(tag, dict) and tag.get("term")
        ],
    }


class Collector:
    def __init__(self, settings: Settings, db: Database, log: PipelineLog | None = None):
        self.settings = settings
        self.db = db
        self.log = log
        self._last_skip_reasons: dict[str, int] = {}
        self._last_domain_fetch: dict[str, float] = {}
        self._robots_cache: dict[
            str, tuple[urllib.robotparser.RobotFileParser | None, bool, float]
        ] = {}

    def sync_sources(self) -> None:
        with self.db.transaction() as connection:
            for source in self.settings.sources:
                connection.execute(
                    """INSERT INTO source(id,name,kind,url,station,enabled,config_json)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,kind=excluded.kind,url=excluded.url,station=excluded.station,
                    enabled=excluded.enabled,config_json=excluded.config_json""",
                    (
                        source["id"],
                        source["name"],
                        source["kind"],
                        source["url"],
                        source.get("station", "brasserie"),
                        int(source.get("enabled", True)),
                        json.dumps(source, ensure_ascii=False),
                    ),
                )

    def _robots_policy(
        self, url: str
    ) -> tuple[urllib.robotparser.RobotFileParser | None, bool, float]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None, False, 0.0
        authority = f"{parsed.scheme}://{parsed.netloc}"
        if authority in self._robots_cache:
            return self._robots_cache[authority]
        robots_url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, "/robots.txt", "", "", "")
        )
        try:
            response = httpx.get(
                robots_url,
                headers={"User-Agent": self.settings.user_agent},
                timeout=15,
                follow_redirects=True,
            )
        except httpx.HTTPError:
            policy = (None, False, 0.0)
        else:
            if 400 <= response.status_code < 500:
                # RFC 9309 §2.3.1.3 : robots.txt indisponible, accès autorisé.
                policy = (None, response.status_code != 429, 0.0)
            elif response.status_code >= 500:
                # RFC 9309 §2.3.1.4 : serveur injoignable, interdiction complète.
                policy = (None, False, 0.0)
            else:
                response.raise_for_status()
                parser = urllib.robotparser.RobotFileParser(robots_url)
                parser.parse(response.text[:512_000].splitlines())
                delay = parser.crawl_delay(self.settings.user_agent)
                if delay is None:
                    delay = parser.crawl_delay("*")
                policy = (parser, True, float(delay or 0))
        self._robots_cache[authority] = policy
        return policy

    def _allowed(self, url: str) -> bool:
        parser, default_allowed, _ = self._robots_policy(url)
        if parser is None:
            return default_allowed
        return parser.can_fetch(self.settings.user_agent, url)

    def _effective_delay(self, url: str, configured_delay: float) -> float:
        _, _, robots_delay = self._robots_policy(url)
        return max(configured_delay, robots_delay)

    def _wait(self, url: str, delay: float) -> None:
        domain = urllib.parse.urlparse(url).netloc
        elapsed = time.monotonic() - self._last_domain_fetch.get(domain, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_domain_fetch[domain] = time.monotonic()

    def collect_all(self) -> dict[str, Any]:
        self.sync_sources()
        result: dict[str, Any] = {
            "sources": 0,
            "items": 0,
            "skipped": 0,
            "skipped_reasons": {},
            "failed_sources": [],
            "details": [],
        }
        for source in self.settings.sources:
            if not source.get("enabled", True):
                continue
            result["sources"] += 1
            if self.log:
                self.log.info(
                    "collect.source.start",
                    "Collecte de la source",
                    source=source["id"],
                    kind=source["kind"],
                    url=sanitize_url(source["url"]),
                )
            started_at = dt.datetime.now(dt.UTC).isoformat()
            fetch_id = self.db.execute(
                "INSERT INTO source_fetch(source_id,started_at) VALUES(?,?)",
                (source["id"], started_at),
            )
            try:
                self._last_skip_reasons = {}
                count, skipped = self.collect_source(source)
                skipped_reasons = self._last_skip_reasons
                result["items"] += count
                result["skipped"] += skipped
                aggregate_reasons = Counter(result["skipped_reasons"])
                aggregate_reasons.update(skipped_reasons)
                result["skipped_reasons"] = dict(aggregate_reasons)
                finished_at = dt.datetime.now(dt.UTC).isoformat()
                self.db.execute(
                    """UPDATE source_fetch
                    SET finished_at=?,status_code=200,item_count=? WHERE id=?""",
                    (finished_at, count, fetch_id),
                )
                self.db.execute(
                    "UPDATE source SET last_fetch_at=? WHERE id=?",
                    (finished_at, source["id"]),
                )
                result["details"].append(
                    {
                        "source": source["id"],
                        "status": "ok",
                        "items": count,
                        "skipped": skipped,
                        "skipped_reasons": skipped_reasons,
                    }
                )
                if self.log:
                    self.log.info(
                        "collect.source.done",
                        "Source collectée",
                        source=source["id"],
                        items=count,
                        skipped=skipped,
                        skipped_reasons=skipped_reasons,
                    )
            except Exception as exc:
                error = str(redact(str(exc)))
                self.db.execute(
                    """UPDATE source_fetch
                    SET finished_at=?,error=? WHERE id=?""",
                    (dt.datetime.now(dt.UTC).isoformat(), error, fetch_id),
                )
                self.db.execute(
                    "INSERT INTO incident(kind,message,context_json) VALUES(?,?,?)",
                    ("collect", error, json.dumps({"source": source["id"]})),
                )
                result["failed_sources"].append(source["id"])
                result["details"].append(
                    {"source": source["id"], "status": "error", "error": error}
                )
                if self.log:
                    self.log.exception(
                        "collect.source.error",
                        "Échec de la source",
                        source=source["id"],
                        error=error,
                    )
        self.purge_raw_text()
        return result

    def collect_source(self, source: dict[str, Any]) -> tuple[int, int]:
        inserted, skipped, reasons = self._collect_source_detailed(source)
        self._last_skip_reasons = reasons
        return inserted, skipped

    def _collect_source_detailed(self, source: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
        if source["kind"] != "rss":
            raise NotImplementedError(f"Collecteur {source['kind']} à configurer")
        if not self._allowed(source["url"]):
            raise PermissionError(f"robots.txt interdit {source['url']}")
        import feedparser
        import trafilatura

        configured_delay = float(source.get("min_interval_seconds", 10))
        self._wait(source["url"], self._effective_delay(source["url"], configured_delay))
        response = httpx.get(
            source["url"],
            headers={"User-Agent": self.settings.user_agent},
            timeout=30,
            follow_redirects=True,
        )
        response.raise_for_status()
        final_feed_url = str(response.url)
        if final_feed_url != source["url"] and not self._allowed(final_feed_url):
            raise PermissionError(f"robots.txt interdit {final_feed_url}")
        feed = feedparser.parse(response.content)
        if not feed.entries and getattr(feed, "bozo", False):
            raise ValueError(f"Flux RSS invalide: {getattr(feed, 'bozo_exception', 'inconnu')}")
        now = dt.datetime.now(dt.UTC)
        import_history = str(source.get("history", "recent")).casefold() == "all"
        max_age = None
        if not import_history:
            max_age = dt.timedelta(
                days=int(
                    source.get("max_age_days", self.settings.radio.get("item_max_age_days", 21))
                )
            )
        inserted = skipped = 0
        skipped_reasons: Counter[str] = Counter()
        for entry in feed.entries:
            url = entry.get("link")
            published = _entry_date(entry)
            parsed_url = urllib.parse.urlparse(url or "")
            if not url or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                skipped_reasons["invalid_url"] += 1
                skipped += 1
                continue
            if not published:
                skipped_reasons["missing_date"] += 1
                skipped += 1
                continue
            if published > now + dt.timedelta(days=1):
                skipped_reasons["future_date"] += 1
                skipped += 1
                continue
            if max_age is not None and now - published > max_age:
                skipped_reasons["too_old"] += 1
                skipped += 1
                continue
            if self.db.one("SELECT id FROM item WHERE url=?", (url,)):
                skipped_reasons["duplicate"] += 1
                skipped += 1
                continue
            feed_text = _entry_text(entry)
            text = feed_text if source.get("prefer_feed_content", False) else ""
            page_error = None
            if len(text) < 120 and self._allowed(url):
                try:
                    self._wait(url, self._effective_delay(url, configured_delay))
                    page = httpx.get(
                        url,
                        headers={"User-Agent": self.settings.user_agent},
                        timeout=30,
                        follow_redirects=True,
                    )
                    page.raise_for_status()
                    final_page_url = str(page.url)
                    if final_page_url == url or self._allowed(final_page_url):
                        text = (
                            trafilatura.extract(
                                page.text, include_comments=False, include_tables=False
                            )
                            or ""
                        )
                except (httpx.HTTPError, ValueError) as exc:
                    page_error = str(exc)
            if len(text) < 120:
                text = feed_text
            if len(text) < 120:
                if page_error:
                    self.db.execute(
                        "INSERT INTO incident(kind,message,context_json) VALUES(?,?,?)",
                        (
                            "collect_item",
                            page_error,
                            json.dumps({"source": source["id"], "url": sanitize_url(url)}),
                        ),
                    )
                skipped_reasons["short_content"] += 1
                skipped += 1
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.db.execute(
                """INSERT INTO item(source_id,url,title,author,published_at,collected_at,content_hash,raw_text,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    source["id"],
                    url,
                    entry.get("title") or "Sans titre",
                    entry.get("author"),
                    published.isoformat(),
                    now.isoformat(),
                    digest,
                    text,
                    json.dumps(_entry_metadata(entry), ensure_ascii=False),
                ),
            )
            inserted += 1
        return inserted, skipped, dict(skipped_reasons)

    def purge_raw_text(self) -> int:
        days = int(self.settings.radio.get("raw_text_retention_days", 30))
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                "UPDATE item SET raw_text=NULL WHERE collected_at < ? AND raw_text IS NOT NULL",
                (threshold,),
            )
            return cursor.rowcount
