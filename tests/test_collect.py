import datetime as dt
from types import SimpleNamespace

import feedparser
import httpx
import trafilatura

from bsc_radio.collect import Collector
from bsc_radio.db import Database
from tests.helpers import settings


class FakeResponse:
    def __init__(self, *, content=b"", text=""):
        self.content = content
        self.text = text

    def raise_for_status(self):
        return None


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
    assert db.one(
        "SELECT error FROM source_fetch WHERE source_id='broken' ORDER BY id DESC LIMIT 1"
    )["error"] == "flux cassé"
