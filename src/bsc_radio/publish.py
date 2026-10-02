from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from .config import Settings
from .db import Database


def _page(title: str, body: str, settings: Settings, description: str = "") -> str:
    radio = settings.radio
    return f"""<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<meta name=\"description\" content=\"{html.escape(description or radio["tagline"])}\">
<meta property=\"og:title\" content=\"{html.escape(title)}\"><meta property=\"og:description\" content=\"{html.escape(description)}\">
<title>{html.escape(title)} · {html.escape(radio["name"])}</title><link rel=\"stylesheet\" href=\"/assets/app.css\"></head>
<body><header><a class=\"brand\" href=\"/\">{html.escape(radio["name"])}</a><nav><a href=\"/#direct\">Direct</a><a href=\"/#replays\">Replays</a><a href=\"/collecte.html\">Collecte</a><a href=\"/avis.html\">Votre avis</a></nav></header>
<main>{body}</main><footer><p>{html.escape(radio["health_notice"])}</p><p>Radio écrite et lue par des intelligences artificielles. Personnages fictifs.</p></footer>
<div class=\"player\"><button id=\"play\" aria-label=\"Écouter le direct\">▶</button><div><strong>En direct</strong><span id=\"now\">Chargement…</span></div><audio id=\"audio\" src=\"/live.mp3\"></audio></div>
<script src=\"/assets/app.js\"></script></body></html>"""


class Publisher:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def publish(self) -> dict[str, int]:
        self.settings.site_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.site_dir / "episodes").mkdir(exist_ok=True)
        (self.settings.site_dir / "events").mkdir(exist_ok=True)
        (self.settings.site_dir / "data").mkdir(exist_ok=True)
        (self.settings.site_dir / "audio").mkdir(exist_ok=True)
        episodes = self.db.all(
            """SELECT e.*,a.path,a.duration_seconds FROM episode e JOIN audio a ON a.episode_id=e.id
            WHERE e.status IN ('audio','published') AND e.hidden=0 ORDER BY e.created_at DESC"""
        )
        cards = []
        search = []
        sitemap_urls = [
            "/",
            "/collecte.html",
            "/avis.html",
            "/audience.html",
            "/confidentialite.html",
        ]
        podcast_items = []
        expected_episode_pages: set[str] = set()
        expected_audio_files: set[str] = set()
        for episode in episodes:
            script = json.loads(episode["script_json"])
            transcript = " ".join(line["text"] for line in script)
            audio_name = Path(episode["path"]).name
            expected_audio_files.add(audio_name)
            expected_episode_pages.add(f"{episode['slug']}.html")
            source = Path(episode["path"])
            if source.exists():
                shutil.copy2(source, self.settings.site_dir / "audio" / audio_name)
            sentences = self.db.all(
                "SELECT * FROM sentence WHERE episode_id=? AND verdict!='deleted' ORDER BY position",
                (episode["id"],),
            )
            paragraphs = []
            sources: dict[str, str] = {}
            for sentence in sentences:
                ids = json.loads(sentence["fact_ids_json"])
                facts = (
                    self.db.all(
                        f"SELECT * FROM fact WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids)
                    )
                    if ids
                    else []
                )
                refs = " ".join(
                    f'<a href="{html.escape(f["source_url"])}" rel="noreferrer">[{f["source_name"]}]</a>'
                    for f in facts
                )
                for fact in facts:
                    sources[fact["source_url"]] = fact["source_name"]
                paragraphs.append(
                    f"<p><strong>{html.escape(sentence['character'])}</strong> {html.escape(sentence['text'])} {refs}</p>"
                )
            body = f'<article><p class="eyebrow">{html.escape(episode["format"])}</p><h1>{html.escape(episode["title"])}</h1><p>{html.escape(episode["summary"])}</p><audio controls src="/audio/{audio_name}"></audio><h2>Transcription sourcée</h2>{"".join(paragraphs)}</article>'
            (self.settings.site_dir / "episodes" / f"{episode['slug']}.html").write_text(
                _page(episode["title"], body, self.settings, episode["summary"]), encoding="utf-8"
            )
            cards.append(
                f'<article class="card"><span>{html.escape(episode["format"])}</span><h3><a href="/episodes/{episode["slug"]}.html">{html.escape(episode["title"])}</a></h3><p>{html.escape(episode["summary"])}</p></article>'
            )
            search.append(
                {
                    "title": episode["title"],
                    "summary": episode["summary"],
                    "text": transcript,
                    "url": f"/episodes/{episode['slug']}.html",
                }
            )
            sitemap_urls.append(f"/episodes/{episode['slug']}.html")
            podcast_items.append(
                f"<item><title>{html.escape(episode['title'])}</title>"
                f"<guid>{self.settings.site_url}/episodes/{episode['slug']}.html</guid>"
                f"<link>{self.settings.site_url}/episodes/{episode['slug']}.html</link>"
                f"<description>{html.escape(episode['summary'])}</description>"
                f'<enclosure url="{self.settings.site_url}/audio/{audio_name}" type="audio/mpeg" />'
                f"</item>"
            )
            self.db.execute(
                "UPDATE episode SET status='published',published_at=COALESCE(published_at,CURRENT_TIMESTAMP) WHERE id=?",
                (episode["id"],),
            )
        for path in (self.settings.site_dir / "episodes").glob("*.html"):
            if path.name not in expected_episode_pages:
                path.unlink()
        for path in (self.settings.site_dir / "audio").glob("*.mp3"):
            if path.name not in expected_audio_files:
                path.unlink()
        body = f"""<section class=\"hero\"><p class=\"eyebrow\">EN CONSTRUCTION · 100 % SOURCÉE</p><h1>{html.escape(self.settings.radio["tagline"])}</h1><p>Actualité, technique, styles et histoire. Chaque phrase renvoie à ses sources.</p></section><section id=\"direct\"><h2>Le direct</h2><p>Une antenne continue en MP3 128 kb/s, accessible sans compte.</p><button class=\"primary\" onclick=\"document.querySelector('#play').click()\">Écouter maintenant</button></section><section id=\"replays\"><h2>Dernières émissions</h2><div class=\"grid\">{"".join(cards) or "<p>Les premières émissions arrivent.</p>"}</div></section>"""
        (self.settings.site_dir / "index.html").write_text(
            _page(self.settings.radio["name"], body, self.settings), encoding="utf-8"
        )
        (self.settings.site_dir / "data/search.json").write_text(
            json.dumps(search, ensure_ascii=False), encoding="utf-8"
        )
        podcast = (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>{html.escape(self.settings.radio['name'])}</title>"
            f"<link>{self.settings.site_url}</link><description>"
            f"{html.escape(self.settings.radio['tagline'])}</description>{''.join(podcast_items)}"
            "</channel></rss>"
        )
        (self.settings.site_dir / "podcast.xml").write_text(podcast, encoding="utf-8")
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(
                f"<url><loc>{self.settings.site_url}{path}</loc></url>" for path in sitemap_urls
            )
            + "</urlset>"
        )
        (self.settings.site_dir / "sitemap.xml").write_text(sitemap, encoding="utf-8")
        return {"episodes": len(episodes)}
