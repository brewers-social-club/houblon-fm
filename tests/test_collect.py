import datetime as dt
import json
from types import SimpleNamespace

import feedparser
import httpx
import trafilatura

from bsc_radio.collect import Collector
from bsc_radio.db import Database
from tests.helpers import settings


class FakeResponse:
    def __init__(self, *, content=b"", text="", status_code=200, url="https://source.test"):
        self.content = content
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("erreur", request=request, response=response)


class FakeJsonResponse(FakeResponse):
    def __init__(self, payload, *, status_code=200, url="https://api.test"):
        super().__init__(status_code=status_code, url=url)
        self.payload = payload

    def json(self):
        return self.payload


def insert_source(db, source):
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (
            source["id"],
            source["name"],
            source["kind"],
            source["url"],
            source.get("station", "brasserie"),
        ),
    )


def test_collects_feed_with_feedparser_parse_and_redirects(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "source",
        "name": "Source",
        "kind": "rss",
        "url": "https://source.test/feed",
        "station": "brassage",
        "enabled": True,
        "min_interval_seconds": 0,
    }
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (source["id"], source["name"], source["kind"], source["url"], source["station"]),
    )
    published = dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    parsed = SimpleNamespace(
        entries=[
            {
                "link": "https://source.test/article",
                "published": published,
                "title": "Article récent",
                "author": "Auteur",
            }
        ]
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/feed"):
            return FakeResponse(content=b"rss")
        return FakeResponse(text="page")

    monkeypatch.setattr(feedparser, "parse", lambda content: parsed)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(trafilatura, "extract", lambda *args, **kwargs: "x" * 150)
    collector = Collector(cfg, db)
    monkeypatch.setattr(collector, "_allowed", lambda url: True)
    monkeypatch.setattr(collector, "_effective_delay", lambda url, delay: delay)

    inserted, skipped = collector.collect_source(source)

    assert (inserted, skipped) == (1, 0)
    assert calls[0][1]["follow_redirects"] is True
    assert db.one("SELECT COUNT(*) count FROM item")["count"] == 1


def test_collect_all_records_successes_and_failures(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    collector = Collector(cfg, db)
    monkeypatch.setattr(
        cfg.__class__,
        "sources",
        property(
            lambda self: [
                {
                    "id": "ok",
                    "name": "OK",
                    "kind": "rss",
                    "url": "https://ok.test/feed",
                    "enabled": True,
                },
                {
                    "id": "broken",
                    "name": "Broken",
                    "kind": "rss",
                    "url": "https://broken.test/feed",
                    "enabled": True,
                },
            ]
        ),
    )

    def fake_collect(source):
        if source["id"] == "broken":
            raise RuntimeError("flux cassé")
        return 2, 3

    monkeypatch.setattr(collector, "collect_source", fake_collect)

    result = collector.collect_all()

    assert result["items"] == 2
    assert result["failed_sources"] == ["broken"]
    assert db.one("SELECT COUNT(*) count FROM source_fetch")["count"] == 2
    assert (
        db.one("SELECT error FROM source_fetch WHERE source_id='broken' ORDER BY id DESC LIMIT 1")[
            "error"
        ]
        == "flux cassé"
    )


def test_collect_reports_aggregated_skip_reasons(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "source",
        "name": "Source",
        "kind": "rss",
        "url": "https://source.test/feed",
        "station": "brassage",
        "enabled": True,
        "max_age_days": 21,
        "min_interval_seconds": 0,
    }
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (source["id"], source["name"], source["kind"], source["url"], source["station"]),
    )
    recent = dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=90)).strftime("%a, %d %b %Y %H:%M:%S %z")
    db.execute(
        """INSERT INTO item(source_id,url,title,published_at,collected_at,content_hash)
        VALUES(?,?,?,?,?,?)""",
        (source["id"], "https://source.test/known", "Connu", recent, recent, "known"),
    )
    parsed = SimpleNamespace(
        entries=[
            {"link": "https://source.test/known", "published": recent},
            {"link": "https://source.test/old", "published": old},
            {"link": "https://source.test/no-date"},
            {"link": "not-a-url", "published": recent},
        ],
        bozo=False,
    )
    monkeypatch.setattr(feedparser, "parse", lambda _content: parsed)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_kwargs: FakeResponse(content=b"rss", url=url),
    )
    collector = Collector(cfg, db)
    monkeypatch.setattr(collector, "_allowed", lambda _url: True)
    monkeypatch.setattr(collector, "_effective_delay", lambda _url, delay: delay)

    assert collector.collect_source(source) == (0, 4)
    assert collector._last_skip_reasons == {
        "duplicate": 1,
        "too_old": 1,
        "missing_date": 1,
        "invalid_url": 1,
    }


def test_global_null_max_age_imports_old_items(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "archive",
        "name": "Archive",
        "kind": "rss",
        "url": "https://source.test/feed",
        "station": "brassage",
        "enabled": True,
        "content_mode": "feed",
        "min_interval_seconds": 0,
    }
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (source["id"], source["name"], source["kind"], source["url"], source["station"]),
    )
    parsed = SimpleNamespace(
        entries=[
            {
                "link": "https://source.test/ancien-article",
                "published": "Mon, 01 Jan 2018 12:00:00 +0000",
                "title": "Ancien article",
                "summary": "Contenu brassicole historique suffisamment détaillé. " * 5,
            }
        ],
        bozo=False,
    )
    monkeypatch.setattr(feedparser, "parse", lambda _content: parsed)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_kwargs: FakeResponse(content=b"rss", url=url),
    )
    collector = Collector(cfg, db)
    monkeypatch.setattr(collector, "_allowed", lambda _url: True)
    monkeypatch.setattr(collector, "_effective_delay", lambda _url, delay: delay)

    assert collector.collect_source(source) == (1, 0)
    assert collector._last_skip_reasons == {}


def test_podcast_can_import_full_history_from_feed_content(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "podcast",
        "name": "Podcast",
        "kind": "rss",
        "url": "https://feed.example.test/show",
        "station": "degustation",
        "enabled": True,
        "history": "all",
        "content_mode": "feed",
        "min_interval_seconds": 0,
    }
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (source["id"], source["name"], source["kind"], source["url"], source["station"]),
    )
    parsed = SimpleNamespace(
        entries=[
            {
                "id": "episode-guid",
                "link": "https://podcast.example.test/episode",
                "published": "Mon, 01 Jan 2018 12:00:00 +0000",
                "title": "Ancien épisode",
                "author": "La radio",
                "summary": "Description détaillée de l'épisode avec suffisamment de contexte "
                + "brassicole et narratif pour être exploitée correctement. " * 3,
                "content": [
                    {
                        "value": "Description détaillée de l'épisode avec suffisamment de contexte "
                        + "brassicole et narratif pour être exploitée correctement. " * 3
                    }
                ],
                "itunes_duration": "1:02:03",
                "itunes_season": "2",
                "itunes_episode": "7",
                "enclosures": [
                    {
                        "href": "https://audio.example.test/episode.mp3?t=cache",
                        "type": "audio/mpeg",
                        "length": "123456",
                    }
                ],
                "image": {"href": "https://image.example.test/cover.jpg?t=cache"},
                "tags": [{"term": "bière"}, {"term": "cinéma"}],
            }
        ],
        bozo=False,
    )
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return FakeResponse(content=b"rss", url=url)

    monkeypatch.setattr(feedparser, "parse", lambda _content: parsed)
    monkeypatch.setattr(httpx, "get", fake_get)
    collector = Collector(cfg, db)
    monkeypatch.setattr(collector, "_allowed", lambda _url: True)
    monkeypatch.setattr(collector, "_effective_delay", lambda _url, delay: delay)

    assert collector.collect_source(source) == (1, 0)
    assert calls == [source["url"]]
    row = db.one("SELECT raw_text,metadata_json FROM item")
    metadata = json.loads(row["metadata_json"])
    assert "Description détaillée" in row["raw_text"]
    assert row["raw_text"].count("Description détaillée") == 1
    assert metadata == {
        "guid": "episode-guid",
        "audio_url": "https://audio.example.test/episode.mp3",
        "audio_type": "audio/mpeg",
        "audio_bytes": 123456,
        "duration": "1:02:03",
        "season": "2",
        "episode": "7",
        "episode_type": None,
        "explicit": None,
        "image_url": "https://image.example.test/cover.jpg",
        "tags": ["bière", "cinéma"],
    }


def test_robots_uses_radio_user_agent_and_parses_rules(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(
            text="User-agent: *\nDisallow: /private\nAllow: /",
            url="https://source.test/robots.txt",
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    collector = Collector(cfg, db)

    assert collector._allowed("https://source.test/feed") is True
    assert collector._allowed("https://source.test/private/article") is False
    assert calls[0][1]["headers"]["User-Agent"] == cfg.user_agent
    assert len(calls) == 1


def test_robots_unavailable_allows_but_unreachable_denies(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: FakeResponse(status_code=403, url=url),
    )
    assert Collector(cfg, db)._allowed("https://missing.test/feed") is True

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: FakeResponse(status_code=503, url=url),
    )
    assert Collector(cfg, db)._allowed("https://broken.test/feed") is False


def test_article_failure_falls_back_to_feed_summary(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "source",
        "name": "Source",
        "kind": "rss",
        "url": "https://source.test/feed",
        "station": "brassage",
        "enabled": True,
        "min_interval_seconds": 0,
    }
    db.execute(
        "INSERT INTO source(id,name,kind,url,station) VALUES(?,?,?,?,?)",
        (source["id"], source["name"], source["kind"], source["url"], source["station"]),
    )
    published = dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    parsed = SimpleNamespace(
        entries=[
            {
                "link": "https://source.test/article",
                "published": published,
                "title": None,
                "summary": "<p>" + "Résumé exploitable du flux. " * 10 + "</p>",
            }
        ],
        bozo=False,
    )

    def fake_get(url, **kwargs):
        if url.endswith("/feed"):
            return FakeResponse(content=b"rss", url=url)
        return FakeResponse(status_code=500, url=url)

    monkeypatch.setattr(feedparser, "parse", lambda content: parsed)
    monkeypatch.setattr(httpx, "get", fake_get)
    collector = Collector(cfg, db)
    monkeypatch.setattr(collector, "_allowed", lambda url: True)
    monkeypatch.setattr(collector, "_effective_delay", lambda url, delay: delay)

    assert collector.collect_source(source) == (1, 0)
    row = db.one("SELECT title,raw_text FROM item")
    assert row["title"] == "Sans titre"
    assert "Résumé exploitable" in row["raw_text"]


def test_youtube_public_feed_works_without_api_key(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "youtube",
        "name": "YouTube",
        "kind": "youtube",
        "url": "https://www.youtube.com/channel/UC123",
        "channel_id": "UC123",
        "history": "all",
        "min_content_chars": 10,
    }
    insert_source(db, source)
    published = dt.datetime.now(dt.UTC).isoformat()
    parsed = SimpleNamespace(
        entries=[
            {
                "yt_videoid": "video123",
                "link": "https://www.youtube.com/watch?v=video123",
                "published": published,
                "title": "Une nouvelle bière",
                "summary": "Présentation détaillée de la nouvelle bière.",
                "author": "La chaîne",
            }
        ]
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(content=b"atom", url=url)

    monkeypatch.setattr(feedparser, "parse", lambda _content: parsed)
    monkeypatch.setattr(httpx, "get", fake_get)

    assert Collector(cfg, db).collect_source(source) == (1, 0)
    assert calls[0][0].endswith("channel_id=UC123")
    metadata = json.loads(db.one("SELECT metadata_json FROM item")["metadata_json"])
    assert metadata["collection_mode"] == "public_feed"


def test_youtube_data_api_paginates_without_key_in_url(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "youtube_api_key", "youtube-secret")
    db = Database(cfg.database)
    db.migrate()
    source = {
        "id": "youtube-api",
        "name": "YouTube API",
        "kind": "youtube",
        "url": "https://www.youtube.com/channel/UC123",
        "channel_id": "UC123",
        "history": "all",
        "max_pages": 2,
        "min_content_chars": 10,
    }
    insert_source(db, source)
    calls = []
    pages = iter(
        [
            {
                "items": [
                    {
                        "contentDetails": {"videoId": "one"},
                        "snippet": {
                            "title": "Vidéo une",
                            "description": "Description suffisamment longue",
                            "publishedAt": dt.datetime.now(dt.UTC).isoformat(),
                            "channelTitle": "Chaîne",
                        },
                    }
                ],
                "nextPageToken": "next",
            },
            {"items": []},
        ]
    )

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/channels"):
            return FakeJsonResponse(
                {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UP123"}}}]},
                url=url,
            )
        return FakeJsonResponse(next(pages), url=url)

    monkeypatch.setattr(httpx, "get", fake_get)

    assert Collector(cfg, db).collect_source(source) == (1, 0)
    assert len(calls) == 3
    assert all(call[1]["headers"]["X-Goog-Api-Key"] == "youtube-secret" for call in calls)
    assert all("key" not in call[1].get("params", {}) for call in calls)


def test_meta_collectors_use_bearer_header_and_cursor(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "meta_access_token", "meta-secret")
    db = Database(cfg.database)
    db.migrate()
    now = dt.datetime.now(dt.UTC).isoformat()
    calls = []
    responses = iter(
        [
            {
                "data": [
                    {
                        "id": "page_1",
                        "message": "Une actualité brassicole Facebook.",
                        "created_time": now,
                        "permalink_url": "https://www.facebook.com/page/posts/1",
                    }
                ],
                "paging": {"cursors": {"after": "cursor-2"}},
            },
            {"data": []},
            {
                "data": [
                    {
                        "id": "ig_1",
                        "caption": "Une actualité brassicole Instagram.",
                        "media_type": "IMAGE",
                        "permalink": "https://www.instagram.com/p/ABC/",
                        "timestamp": now,
                    }
                ]
            },
        ]
    )

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeJsonResponse(next(responses), url=url)

    monkeypatch.setattr(httpx, "get", fake_get)
    collector = Collector(cfg, db)
    facebook = {
        "id": "facebook",
        "name": "Facebook",
        "kind": "facebook",
        "url": "https://www.facebook.com/page",
        "page_id": "page",
        "history": "all",
    }
    instagram = {
        "id": "instagram",
        "name": "Instagram",
        "kind": "instagram",
        "url": "https://www.instagram.com/account/",
        "ig_user_id": "ig-user",
        "history": "all",
    }
    insert_source(db, facebook)
    insert_source(db, instagram)

    assert collector.collect_source(facebook) == (1, 0)
    assert collector.collect_source(instagram) == (1, 0)
    assert calls[1][1]["params"]["after"] == "cursor-2"
    assert all(call[1]["headers"]["Authorization"] == "Bearer meta-secret" for call in calls)
    assert all("meta-secret" not in call[0] for call in calls)


def test_google_grounded_stores_summary_and_citations(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "gemini_api_key", "gemini-secret")
    db = Database(cfg.database)
    db.migrate()
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeJsonResponse(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Actualités brassicoles vérifiées et datées."}]},
                        "groundingMetadata": {
                            "webSearchQueries": ["actualités bière France"],
                            "groundingChunks": [
                                {"web": {"title": "Beer News", "uri": "https://beernews.fr/a?token=x"}}
                            ],
                        },
                    }
                ]
            },
            url=url,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    source = {
        "id": "google",
        "name": "Veille Google",
        "kind": "google_grounded",
        "url": "https://www.google.com/search?q=biere",
        "query": "actualités bière France",
        "min_content_chars": 20,
    }
    insert_source(db, source)

    assert Collector(cfg, db).collect_source(source) == (1, 0)
    assert calls[0][1]["headers"]["x-goog-api-key"] == "gemini-secret"
    assert "gemini-secret" not in calls[0][0]
    item = db.one("SELECT url,metadata_json FROM item")
    assert "radio_date=" in item["url"]
    metadata = json.loads(item["metadata_json"])
    assert metadata["citations"] == [{"title": "Beer News", "url": "https://beernews.fr/a"}]
