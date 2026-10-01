from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import time
import urllib.parse
import urllib.robotparser
from typing import Any

from .config import Settings
from .db import Database


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


class Collector:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._last_domain_fetch: dict[str, float] = {}

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

    def _allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        robots_url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, "/robots.txt", "", "", "")
        )
        parser = urllib.robotparser.RobotFileParser(robots_url)
        try:
            parser.read()
            return parser.can_fetch(self.settings.user_agent, url)
        except OSError:
            return False

    def _wait(self, url: str, delay: float) -> None:
        domain = urllib.parse.urlparse(url).netloc
        elapsed = time.monotonic() - self._last_domain_fetch.get(domain, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_domain_fetch[domain] = time.monotonic()

    def collect_all(self) -> dict[str, int]:
        self.sync_sources()
        result = {"sources": 0, "items": 0, "skipped": 0}
        for source in self.settings.sources:
            if not source.get("enabled", True):
                continue
            result["sources"] += 1
            try:
                count, skipped = self.collect_source(source)
                result["items"] += count
                result["skipped"] += skipped
            except Exception as exc:
                self.db.execute(
                    "INSERT INTO incident(kind,message,context_json) VALUES(?,?,?)",
                    ("collect", str(exc), json.dumps({"source": source["id"]})),
                )
        self.purge_raw_text()
        return result

    def collect_source(self, source: dict[str, Any]) -> tuple[int, int]:
        if source["kind"] != "rss":
            raise NotImplementedError(f"Collecteur {source['kind']} à configurer")
        if not self._allowed(source["url"]):
            raise PermissionError(f"robots.txt interdit {source['url']}")
        import feedparser
        import httpx
        import trafilatura

        self._wait(source["url"], float(source.get("min_interval_seconds", 10)))
        response = httpx.get(
            source["url"], headers={"User-Agent": self.settings.user_agent}, timeout=30
        )
        response.raise_for_status()
        feed = feedparser.loads(response.content)
        now = dt.datetime.now(dt.UTC)
        max_age = dt.timedelta(days=int(self.settings.radio.get("item_max_age_days", 21)))
        inserted = skipped = 0
        for entry in feed.entries:
            url = entry.get("link")
            published = _date(entry.get("published") or entry.get("updated"))
            if not url or not published or now - published > max_age:
                skipped += 1
                continue
            if self.db.one("SELECT id FROM item WHERE url=?", (url,)):
                skipped += 1
                continue
            self._wait(url, float(source.get("min_interval_seconds", 10)))
            page = httpx.get(
                url,
                headers={"User-Agent": self.settings.user_agent},
                timeout=30,
                follow_redirects=True,
            )
            page.raise_for_status()
            text = (
                trafilatura.extract(page.text, include_comments=False, include_tables=False) or ""
            )
            if len(text) < 120:
                skipped += 1
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.db.execute(
                """INSERT INTO item(source_id,url,title,author,published_at,collected_at,content_hash,raw_text,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    source["id"],
                    url,
                    entry.get("title", "Sans titre"),
                    entry.get("author"),
                    published.isoformat(),
                    now.isoformat(),
                    digest,
                    text,
                    json.dumps({"language": "unknown"}),
                ),
            )
            inserted += 1
        return inserted, skipped

    def purge_raw_text(self) -> int:
        days = int(self.settings.radio.get("raw_text_retention_days", 30))
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                "UPDATE item SET raw_text=NULL WHERE collected_at < ? AND raw_text IS NOT NULL",
                (threshold,),
            )
            return cursor.rowcount
