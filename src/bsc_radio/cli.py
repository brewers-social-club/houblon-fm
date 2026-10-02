from __future__ import annotations

import datetime as dt
import json
import re

import typer

from .audio import AudioProducer
from .characters import canonicalize_script, character_voice, resolve_character
from .collect import Collector
from .config import Settings
from .db import Database
from .editorial import EditorialPipeline
from .providers import editorial_provider, voice_provider
from .publish import Publisher
from .sidecar import create_app

app = typer.Typer(no_args_is_help=True, help="Houblon FM — pipeline de webradio sourcée")


def _safe_failure_message(exc: Exception) -> str:
    message = re.sub(
        r"\b(?:sk|sess|eyJ)[A-Za-z0-9._-]{12,}\b",
        "[secret redacted]",
        str(exc),
    )
    return message.strip()[-1800:] or exc.__class__.__name__


def _mark_episode_failed(db: Database, episode_id: int, stage: str, exc: Exception) -> str:
    message = _safe_failure_message(exc)
    db.execute(
        """UPDATE episode SET status='failed',rejection_reason=?,updated_at=CURRENT_TIMESTAMP
        WHERE id=?""",
        (f"{stage}: {message}", episode_id),
    )
    db.execute(
        "INSERT INTO incident(kind,message,context_json) VALUES(?,?,?)",
        (
            "pipeline",
            message,
            json.dumps({"episode_id": episode_id, "stage": stage}),
        ),
    )
    return message


def _finish_episode(settings, db, editorial, audio, episode_id: int) -> dict[str, object]:
    episode = db.one("SELECT * FROM episode WHERE id=?", (episode_id,))
    if not episode:
        raise ValueError(f"Épisode #{episode_id} introuvable")
    if episode["status"] == "rejected":
        raise ValueError("Épisode rejeté éditorialement ; corrigez son script avant reprise")
    if episode["status"] == "published":
        return {"status": "published", "episode_id": episode_id, "already_done": True}

    audio_row = db.one("SELECT id,path FROM audio WHERE episode_id=?", (episode_id,))
    stage = "verify"
    try:
        if not audio_row and episode["status"] not in {"verified", "audio"}:
            verdict = editorial.verify_episode(episode_id)
            if verdict["status"] == "rejected":
                return {"status": "rejected", "episode_id": episode_id}
        if not audio_row:
            stage = "audio"
            audio.produce(episode_id)
        elif episode["status"] != "audio":
            db.execute(
                "UPDATE episode SET status='audio',rejection_reason=NULL WHERE id=?",
                (episode_id,),
            )
        stage = "publish"
        Publisher(settings, db).publish()
        return {"status": "published", "episode_id": episode_id, "already_done": False}
    except Exception as exc:
        _mark_episode_failed(db, episode_id, stage, exc)
        raise


def services():
    settings = Settings.load()
    settings.ensure_dirs()
    db = Database(settings.database)
    db.migrate()
    editorial = EditorialPipeline(settings, db, editorial_provider(settings))
    audio = AudioProducer(settings, db, voice_provider(settings))
    return settings, db, editorial, audio


@app.command()
def init():
    """Initialise les répertoires, la base et les sources."""
    settings, db, _, audio = services()
    Collector(settings, db).sync_sources()
    audio.make_jingle(settings.data_dir / "jingle-open.mp3")
    audio.make_jingle(settings.data_dir / "jingle-close.mp3", frequency=392)
    audio.make_jingle(settings.data_dir / "fallback.mp3", seconds=15, frequency=220)
    typer.echo(f"Initialisé: {settings.database}")


@app.command()
def collect():
    settings, db, _, _ = services()
    typer.echo(json.dumps(Collector(settings, db).collect_all(), ensure_ascii=False))


@app.command()
def facts(limit: int = 25):
    _, _, editorial, _ = services()
    extracted = editorial.extract_pending(limit)
    topics_count = editorial.build_topics()
    typer.echo(f"{extracted} articles extraits, {topics_count} sujets créés")


@app.command()
def topics(hours: int = 48):
    _, db, _, _ = services()
    threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
    for row in db.all(
        "SELECT id,title,summary,station FROM topic WHERE last_seen_at>=? ORDER BY last_seen_at DESC",
        (threshold,),
    ):
        typer.echo(f"#{row['id']} [{row['station']}] {row['title']}\n  {row['summary']}")


@app.command()
def run(fmt: str = "journal", hours: int = 48, skip_collect: bool = False):
    settings, db, editorial, audio = services()
    try:
        if not skip_collect:
            Collector(settings, db).collect_all()
        editorial.extract_pending()
        editorial.build_topics()
        episode_id = editorial.write_episode(fmt, hours)
        result = _finish_episode(settings, db, editorial, audio, episode_id)
    except Exception as exc:
        typer.echo(f"Échec du pipeline : {_safe_failure_message(exc)}", err=True)
        raise typer.Exit(1) from exc
    if result["status"] == "rejected":
        typer.echo(f"Épisode #{episode_id} rejeté par la vérification", err=True)
        raise typer.Exit(2)
    row = db.one("SELECT path FROM audio WHERE episode_id=?", (episode_id,))
    typer.echo(f"Épisode #{episode_id}: {row['path']}")


@app.command()
def episodes(
    status: str | None = typer.Option(None, "--status", help="Filtrer par statut"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
):
    """Liste les émissions, y compris les brouillons et les échecs."""
    _, db, _, _ = services()
    where = "WHERE status=?" if status else ""
    params = (status, limit) if status else (limit,)
    rows = db.all(
        f"""SELECT id,title,format,status,created_at,rejection_reason FROM episode
        {where} ORDER BY id DESC LIMIT ?""",
        params,
    )
    if not rows:
        typer.echo("Aucun épisode")
        return
    for row in rows:
        failure = f" — {row['rejection_reason']}" if row["rejection_reason"] else ""
        typer.echo(
            f"#{row['id']} [{row['status']}] {row['format']} — {row['title']} "
            f"({row['created_at']}){failure}"
        )


@app.command()
def resume(episode_id: int):
    """Reprend une émission incomplète sans répéter les étapes déjà terminées."""
    settings, db, editorial, audio = services()
    try:
        result = _finish_episode(settings, db, editorial, audio, episode_id)
    except Exception as exc:
        typer.echo(f"Échec de la reprise #{episode_id} : {_safe_failure_message(exc)}", err=True)
        raise typer.Exit(1) from exc
    if result["status"] == "rejected":
        typer.echo(f"Épisode #{episode_id} rejeté par la vérification", err=True)
        raise typer.Exit(2)
    suffix = " (déjà publié)" if result.get("already_done") else ""
    typer.echo(f"Épisode #{episode_id} publié{suffix}")


@app.command()
def demo():
    """Crée une émission complète sans réseau ni clés API."""
    settings, db, editorial, audio = services()
    Collector(settings, db).sync_sources()
    now = dt.datetime.now(dt.UTC).isoformat()
    db.execute(
        """INSERT OR IGNORE INTO item(source_id,url,title,published_at,collected_at,content_hash,raw_text)
        VALUES(?,?,?,?,?,?,?)""",
        (
            "brulosophy",
            f"https://example.test/demo-{dt.date.today().isoformat()}",
            "Une fermentation plus fraîche et régulière",
            now,
            now,
            "demo-v1",
            "Une brasserie pilote compare deux fermentations. La température stable réduit les écarts aromatiques. Les résultats restent spécifiques à cette recette.",
        ),
    )
    editorial.extract_pending()
    editorial.build_topics()
    episode_id = editorial.write_episode("journal", title="Le Journal de démonstration")
    editorial.verify_episode(episode_id)
    path = audio.produce(episode_id)
    Publisher(settings, db).publish()
    typer.echo(f"Démo #{episode_id}: {path}")


@app.command("voice-queue")
def voice_queue(maximum: int = typer.Option(3, "--max")):
    _, db, _, audio = services()
    rows = db.all(
        "SELECT id FROM episode WHERE status='verified' ORDER BY created_at LIMIT ?", (maximum,)
    )
    for row in rows:
        typer.echo(str(audio.produce(row["id"])))


@app.command()
def revoice(episode_id: int):
    """Revocalise une émission existante avec le fournisseur TTS configuré."""
    settings, db, _, audio = services()
    path = audio.produce(episode_id)
    Publisher(settings, db).publish()
    typer.echo(f"Épisode #{episode_id} revocalisé : {path}")


@app.command()
def say(text: str, character: str = "presenter"):
    settings, _, _, _ = services()
    role, profile = resolve_character(settings.characters, character)
    output = settings.data_dir / "preview" / f"{role}.mp3"
    voice_provider(settings).synthesize(
        text,
        character_voice(
            profile,
            settings.tts_provider,
            settings.elevenlabs_voice_id if settings.tts_provider == "elevenlabs" else "",
        ),
        output,
        profile["description"],
    )
    typer.echo(str(output))


@app.command()
def voices():
    """Affiche le casting TTS résolu pour le fournisseur actif."""
    settings = Settings.load()
    for role in settings.characters:
        _, profile = resolve_character(settings.characters, role)
        voice = character_voice(
            profile,
            settings.tts_provider,
            settings.elevenlabs_voice_id if settings.tts_provider == "elevenlabs" else "",
        )
        typer.echo(f"{role}: {profile.get('name', role)} -> {voice or '(non configurée)'}")


@app.command()
def publish():
    settings, db, _, _ = services()
    typer.echo(json.dumps(Publisher(settings, db).publish(), ensure_ascii=False))


@app.command("import-scripts")
def import_scripts(directory: str = "emissions"):
    """Importe ou remplace des scripts JSON écrits hors API."""
    settings, db, editorial, _ = services()
    imported = 0
    for path in sorted((settings.root / directory).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        slug = document.get("slug") or path.stem
        script = canonicalize_script(document["script"], settings.characters)
        existing = db.one("SELECT id FROM episode WHERE slug=?", (slug,))
        with db.transaction() as connection:
            if existing:
                episode_id = existing["id"]
                connection.execute(
                    """UPDATE episode SET title=?,format=?,station=?,summary=?,script_json=?,
                    status='draft',updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        document["title"],
                        document["format"],
                        document.get("station", "brassage"),
                        document.get("summary", ""),
                        json.dumps(script, ensure_ascii=False),
                        episode_id,
                    ),
                )
                connection.execute("DELETE FROM sentence WHERE episode_id=?", (episode_id,))
            else:
                episode_id = connection.execute(
                    """INSERT INTO episode(slug,title,format,station,summary,script_json)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        slug,
                        document["title"],
                        document["format"],
                        document.get("station", "brassage"),
                        document.get("summary", ""),
                        json.dumps(script, ensure_ascii=False),
                    ),
                ).lastrowid
            connection.executemany(
                "INSERT INTO sentence(episode_id,position,character,text,fact_ids_json) VALUES(?,?,?,?,?)",
                [
                    (
                        episode_id,
                        position,
                        line["character"],
                        line["text"],
                        json.dumps(line.get("fact_ids", [])),
                    )
                    for position, line in enumerate(script)
                ],
            )
        editorial.verify_episode(int(episode_id))
        imported += 1
    typer.echo(f"{imported} script(s) importé(s)")


@app.command()
def stock(fmt: str, all_items: bool = typer.Option(False, "--all")):
    """Produit une émission de stock, ou plusieurs avec --all."""
    _, _, editorial, _ = services()
    count = 3 if all_items else 1
    identifiers = [editorial.write_episode(fmt, hours=24 * 365) for _ in range(count)]
    for identifier in identifiers:
        editorial.verify_episode(identifier)
    typer.echo(json.dumps(identifiers))


@app.command("video-series")
def video_series(list_only: bool = typer.Option(False, "--list")):
    """Liste les séries vidéo configurées (analyse lancée par la collecte dédiée)."""
    settings, _, _, _ = services()
    path = settings.config_dir / "video_series.yaml"
    if not path.exists():
        typer.echo("Aucune série vidéo configurée")
        return
    import yaml

    series = yaml.safe_load(path.read_text(encoding="utf-8")).get("series", [])
    for entry in series:
        typer.echo(f"{entry['id']} — {entry['name']} ({entry['channel_url']})")


@app.command()
def withdraw(episode_id: int):
    _, db, _, _ = services()
    db.execute("UPDATE episode SET hidden=1 WHERE id=?", (episode_id,))
    typer.echo(f"Épisode #{episode_id} retiré")


@app.command()
def feedback():
    _, db, _, _ = services()
    for row in db.all("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 50"):
        typer.echo(f"#{row['id']} {row['rating'] or '-'} {row['message']}")


@app.command()
def airings():
    _, db, _, _ = services()
    for row in db.all(
        "SELECT a.*,e.title FROM airing a LEFT JOIN episode e ON e.id=a.episode_id ORDER BY started_at DESC LIMIT 50"
    ):
        typer.echo(f"{row['started_at']} — {row['title'] or 'secours'}")


@app.command()
def stats():
    _, db, _, _ = services()
    values = {
        "episodes": db.one("SELECT COUNT(*) value FROM episode")["value"],
        "published": db.one("SELECT COUNT(*) value FROM episode WHERE status='published'")["value"],
        "airings": db.one("SELECT COUNT(*) value FROM airing")["value"],
        "cost_eur": db.one("SELECT COALESCE(SUM(cost_micro_eur),0)/1000000.0 value FROM cost")[
            "value"
        ],
    }
    typer.echo(json.dumps(values, ensure_ascii=False, indent=2))


@app.command()
def sidecar(host: str = "0.0.0.0", port: int = 8081):
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    app()
