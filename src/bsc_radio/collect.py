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
    unique_fragments: list[str] = []
    seen: set[str] = set()
    for fragment in fragments:
        normalized = str(fragment or "").strip()
        if normalized and normalized not in seen:
            unique_fragments.append(normalized)
            seen.add(normalized)
    plain = " ".join(unique_fragments)
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
        kind = str(source["kind"]).casefold()
        if kind == "youtube":
            return self._collect_youtube(source)
        if kind == "facebook":
            return self._collect_facebook(source)
        if kind == "instagram":
            return self._collect_instagram(source)
        if kind in {"google_grounded", "google-search-grounded"}:
            return self._collect_google_grounded(source)
        if kind != "rss":
            raise NotImplementedError(f"Collecteur {source['kind']} à configurer")
        return self._collect_rss(source)

    def _collect_rss(self, source: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
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
            configured_max_age = source.get(
                "max_age_days", self.settings.radio.get("item_max_age_days", 21)
            )
            if configured_max_age is not None:
                max_age = dt.timedelta(days=int(configured_max_age))
        inserted = skipped = 0
        skipped_reasons: Counter[str] = Counter()
        total_entries = len(feed.entries)
        for entry_index, entry in enumerate(feed.entries, start=1):
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
            existing = self.db.one(
                "SELECT id,content_hash,metadata_json FROM item WHERE url=?", (url,)
            )
            if existing and not source.get("refresh_existing", False):
                skipped_reasons["duplicate"] += 1
                skipped += 1
                continue
            feed_text = _entry_text(entry)
            content_mode = str(
                source.get(
                    "content_mode",
                    "feed" if source.get("prefer_feed_content", False) else "auto",
                )
            ).casefold()
            minimum_chars = int(source.get("min_content_chars", 120))
            # En mode auto, un contenu RSS suffisamment riche évite une requête de page
            # et son crawl-delay. Les podcasts exposent généralement toute leur description.
            text = feed_text if content_mode in {"feed", "auto"} else ""
            page_error = None
            if content_mode != "feed" and len(text) < minimum_chars and self._allowed(url):
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
            if len(text) < minimum_chars:
                text = feed_text
            if len(text) < minimum_chars:
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
            metadata_json = json.dumps(_entry_metadata(entry), ensure_ascii=False)
            if existing:
                if existing["content_hash"] != digest or existing["metadata_json"] != metadata_json:
                    self.db.execute(
                        """UPDATE item SET title=?,author=?,published_at=?,collected_at=?,
                        content_hash=?,raw_text=?,metadata_json=? WHERE id=?""",
                        (
                            entry.get("title") or "Sans titre",
                            entry.get("author"),
                            published.isoformat(),
                            now.isoformat(),
                            digest,
                            text,
                            metadata_json,
                            existing["id"],
                        ),
                    )
                    skipped_reasons["refreshed"] += 1
                else:
                    skipped_reasons["duplicate"] += 1
                skipped += 1
                continue
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
                    metadata_json,
                ),
            )
            inserted += 1
            if self.log and (entry_index % 25 == 0 or entry_index == total_entries):
                self.log.info(
                    "collect.source.progress",
                    "Progression de la source",
                    source=source["id"],
                    processed=entry_index,
                    total=total_entries,
                    items=inserted,
                    skipped=skipped,
                )
        return inserted, skipped, dict(skipped_reasons)

    def _max_age(self, source: dict[str, Any]) -> dt.timedelta | None:
        if str(source.get("history", "recent")).casefold() == "all":
            return None
        configured = source.get("max_age_days", self.settings.radio.get("item_max_age_days", 21))
        return dt.timedelta(days=int(configured)) if configured is not None else None

    def _store_records(
        self, source: dict[str, Any], records: list[dict[str, Any]]
    ) -> tuple[int, int, dict[str, int]]:
        now = dt.datetime.now(dt.UTC)
        max_age = self._max_age(source)
        minimum_chars = int(source.get("min_content_chars", 20))
        inserted = skipped = 0
        reasons: Counter[str] = Counter()
        for record in records:
            url = str(record.get("url") or "").strip()
            parsed_url = urllib.parse.urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                reasons["invalid_url"] += 1
                skipped += 1
                continue
            published = record.get("published")
            if isinstance(published, str):
                published = _date(published)
            if not isinstance(published, dt.datetime):
                reasons["missing_date"] += 1
                skipped += 1
                continue
            published = published.astimezone(dt.UTC)
            if published > now + dt.timedelta(days=1):
                reasons["future_date"] += 1
                skipped += 1
                continue
            if max_age is not None and now - published > max_age:
                reasons["too_old"] += 1
                skipped += 1
                continue
            text = re.sub(r"\s+", " ", html.unescape(str(record.get("text") or ""))).strip()
            if len(text) < minimum_chars:
                reasons["short_content"] += 1
                skipped += 1
                continue
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(text.encode()).hexdigest()
            existing = self.db.one(
                "SELECT id,content_hash,metadata_json FROM item WHERE url=?", (url,)
            )
            if existing:
                if not source.get("refresh_existing", False):
                    reasons["duplicate"] += 1
                    skipped += 1
                    continue
                if existing["content_hash"] != digest or existing["metadata_json"] != metadata_json:
                    self.db.execute(
                        """UPDATE item SET title=?,author=?,published_at=?,collected_at=?,
                        content_hash=?,raw_text=?,metadata_json=? WHERE id=?""",
                        (
                            record.get("title") or "Sans titre",
                            record.get("author"),
                            published.isoformat(),
                            now.isoformat(),
                            digest,
                            text,
                            metadata_json,
                            existing["id"],
                        ),
                    )
                    reasons["refreshed"] += 1
                else:
                    reasons["duplicate"] += 1
                skipped += 1
                continue
            self.db.execute(
                """INSERT INTO item(source_id,url,title,author,published_at,collected_at,
                content_hash,raw_text,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    source["id"],
                    url,
                    record.get("title") or "Sans titre",
                    record.get("author"),
                    published.isoformat(),
                    now.isoformat(),
                    digest,
                    text,
                    metadata_json,
                ),
            )
            inserted += 1
        return inserted, skipped, dict(reasons)

    @staticmethod
    def _api_payload(response: httpx.Response, provider: str) -> dict[str, Any]:
        if response.status_code >= 400:
            detail = ""
            try:
                error_payload = response.json()
                error = error_payload.get("error") if isinstance(error_payload, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                if message:
                    detail = f": {str(redact(str(message)))[:300]}"
            except (ValueError, json.JSONDecodeError):
                pass
            raise RuntimeError(f"{provider}: HTTP {response.status_code}{detail}")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{provider}: réponse JSON invalide") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{provider}: réponse inattendue")
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else "erreur API"
            raise RuntimeError(f"{provider}: {message}")
        return payload

    def _collect_youtube(self, source: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
        channel_id = str(source.get("channel_id") or "").strip()
        if not channel_id:
            match = re.search(r"/channel/([A-Za-z0-9_-]+)", str(source.get("url", "")))
            channel_id = match.group(1) if match else ""
        if not channel_id:
            raise RuntimeError("YouTube: channel_id manque")
        records: list[dict[str, Any]] = []
        if not self.settings.youtube_api_key:
            import feedparser

            feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            response = httpx.get(
                feed_url,
                headers={"User-Agent": self.settings.user_agent},
                timeout=30,
                follow_redirects=True,
            )
            if response.status_code >= 400:
                if response.status_code == 404:
                    raise RuntimeError(
                        "YouTube RSS indisponible pour cette chaîne; configure YOUTUBE_API_KEY"
                    )
                raise RuntimeError(f"YouTube RSS: HTTP {response.status_code}")
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                video_id = entry.get("yt_videoid") or entry.get("id", "").rsplit(":", 1)[-1]
                description = _entry_text(entry)
                title = str(entry.get("title") or "Vidéo YouTube")
                records.append(
                    {
                        "url": entry.get("link") or f"https://www.youtube.com/watch?v={video_id}",
                        "title": title,
                        "author": entry.get("author"),
                        "published": _entry_date(entry),
                        "text": f"{title}. {description}".strip(),
                        "metadata": {
                            "platform": "youtube",
                            "video_id": video_id,
                            "collection_mode": "public_feed",
                        },
                    }
                )
            return self._store_records(source, records)

        headers = {
            "User-Agent": self.settings.user_agent,
            "X-Goog-Api-Key": self.settings.youtube_api_key,
        }
        channel_response = httpx.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "contentDetails,snippet", "id": channel_id},
            headers=headers,
            timeout=30,
        )
        channel_payload = self._api_payload(channel_response, "YouTube")
        channels = channel_payload.get("items") or []
        if not channels:
            raise RuntimeError("YouTube: chaîne introuvable ou non autorisée")
        uploads = channels[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            raise RuntimeError("YouTube: playlist des publications introuvable")
        page_token = None
        max_pages = max(1, int(source.get("max_pages", 10)))
        for _ in range(max_pages):
            params = {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            response = httpx.get(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params=params,
                headers=headers,
                timeout=30,
            )
            payload = self._api_payload(response, "YouTube")
            for item in payload.get("items") or []:
                snippet = item.get("snippet") or {}
                video_id = (item.get("contentDetails") or {}).get("videoId") or (
                    snippet.get("resourceId") or {}
                ).get("videoId")
                if not video_id:
                    continue
                title = str(snippet.get("title") or "Vidéo YouTube")
                description = str(snippet.get("description") or "")
                thumbnails = snippet.get("thumbnails") or {}
                thumbnail = next(
                    (
                        thumbnails[key].get("url")
                        for key in ("maxres", "standard", "high", "medium", "default")
                        if isinstance(thumbnails.get(key), dict) and thumbnails[key].get("url")
                    ),
                    None,
                )
                records.append(
                    {
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": title,
                        "author": snippet.get("videoOwnerChannelTitle")
                        or snippet.get("channelTitle"),
                        "published": snippet.get("publishedAt"),
                        "text": f"{title}. {description}".strip(),
                        "metadata": {
                            "platform": "youtube",
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "thumbnail_url": sanitize_url(thumbnail) if thumbnail else None,
                            "collection_mode": "data_api",
                        },
                    }
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return self._store_records(source, records)

    def _meta_pages(
        self, object_id: str, edge: str, fields: str, source: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not self.settings.meta_access_token:
            raise RuntimeError("Meta: META_ACCESS_TOKEN manque")
        version = str(source.get("graph_version") or self.settings.meta_graph_version).strip("/")
        endpoint = f"https://graph.facebook.com/{version}/{object_id}/{edge}"
        headers = {
            "Authorization": f"Bearer {self.settings.meta_access_token}",
            "User-Agent": self.settings.user_agent,
        }
        after = None
        output: list[dict[str, Any]] = []
        for _ in range(max(1, int(source.get("max_pages", 5)))):
            params: dict[str, Any] = {"fields": fields, "limit": 100}
            if after:
                params["after"] = after
            response = httpx.get(endpoint, params=params, headers=headers, timeout=30)
            payload = self._api_payload(response, "Meta")
            output.extend(item for item in payload.get("data") or [] if isinstance(item, dict))
            after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
            if not after:
                break
        return output

    def _collect_facebook(self, source: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
        page_id = str(source.get("page_id") or "").strip()
        if not page_id:
            raise RuntimeError("Facebook: page_id manque")
        posts = self._meta_pages(
            page_id,
            "posts",
            "id,message,created_time,permalink_url,attachments{media,type,url,title,description}",
            source,
        )
        records = []
        for post in posts:
            attachments = (post.get("attachments") or {}).get("data") or []
            attachment = attachments[0] if attachments else {}
            text = str(post.get("message") or attachment.get("description") or attachment.get("title") or "")
            post_id = str(post.get("id") or "")
            records.append(
                {
                    "url": post.get("permalink_url") or f"https://www.facebook.com/{post_id}",
                    "title": (text.splitlines()[0][:140] if text else "Publication Facebook"),
                    "author": source.get("name"),
                    "published": post.get("created_time"),
                    "text": text,
                    "metadata": {
                        "platform": "facebook",
                        "post_id": post_id,
                        "attachment_type": attachment.get("type"),
                        "attachment_url": sanitize_url(str(attachment.get("url")))
                        if attachment.get("url")
                        else None,
                    },
                }
            )
        return self._store_records(source, records)

    def _collect_instagram(self, source: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
        user_id = str(source.get("ig_user_id") or "").strip()
        if not user_id:
            raise RuntimeError("Instagram: ig_user_id manque")
        media = self._meta_pages(
            user_id,
            "media",
            "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url",
            source,
        )
        records = []
        for item in media:
            caption = str(item.get("caption") or "")
            media_id = str(item.get("id") or "")
            records.append(
                {
                    "url": item.get("permalink") or f"https://www.instagram.com/p/{media_id}/",
                    "title": caption.splitlines()[0][:140] if caption else "Publication Instagram",
                    "author": source.get("name"),
                    "published": item.get("timestamp"),
                    "text": caption,
                    "metadata": {
                        "platform": "instagram",
                        "media_id": media_id,
                        "media_type": item.get("media_type"),
                        "media_product_type": item.get("media_product_type"),
                        "thumbnail_url": sanitize_url(str(item.get("thumbnail_url")))
                        if item.get("thumbnail_url")
                        else None,
                    },
                }
            )
        return self._store_records(source, records)

    def _collect_google_grounded(
        self, source: dict[str, Any]
    ) -> tuple[int, int, dict[str, int]]:
        if not self.settings.gemini_api_key:
            raise RuntimeError("Google Grounding: GEMINI_API_KEY manque")
        query = str(source.get("query") or "").strip()
        if not query:
            raise RuntimeError("Google Grounding: query manque")
        model = str(source.get("model") or self.settings.gemini_search_model)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
            raise RuntimeError("Google Grounding: modèle invalide")
        prompt = str(
            source.get(
                "prompt",
                "Résume les actualités récentes correspondant à cette recherche. "
                "Reste factuel, indique les dates connues et n'invente rien.",
            )
        )
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={
                "x-goog-api-key": self.settings.gemini_api_key,
                "User-Agent": self.settings.user_agent,
            },
            json={
                "contents": [{"parts": [{"text": f"{prompt}\n\nRecherche : {query}"}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.2},
            },
            timeout=60,
        )
        payload = self._api_payload(response, "Google Grounding")
        candidates = payload.get("candidates") or []
        if not candidates:
            raise RuntimeError("Google Grounding: aucune réponse")
        candidate = candidates[0]
        parts = ((candidate.get("content") or {}).get("parts") or [])
        text = "\n".join(str(part.get("text")) for part in parts if part.get("text")).strip()
        grounding = candidate.get("groundingMetadata") or {}
        citations = []
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if isinstance(web, dict) and web.get("uri"):
                citations.append(
                    {"title": web.get("title"), "url": sanitize_url(str(web["uri"]))}
                )
        collected_on = dt.datetime.now(dt.UTC).date().isoformat()
        separator = "&" if urllib.parse.urlparse(source["url"]).query else "?"
        record = {
            "url": f"{source['url']}{separator}radio_date={collected_on}",
            "title": str(source.get("title") or f"Veille Google du {collected_on} : {query}")[:240],
            "author": "Google Search via Gemini",
            "published": dt.datetime.now(dt.UTC),
            "text": text,
            "metadata": {
                "platform": "google_search_grounding",
                "query": query,
                "model": model,
                "citations": citations,
                "search_queries": grounding.get("webSearchQueries") or [],
            },
        }
        return self._store_records(source, [record])

    def purge_raw_text(self) -> int:
        days = int(self.settings.radio.get("raw_text_retention_days", 30))
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                "UPDATE item SET raw_text=NULL WHERE collected_at < ? AND raw_text IS NOT NULL",
                (threshold,),
            )
            return cursor.rowcount
