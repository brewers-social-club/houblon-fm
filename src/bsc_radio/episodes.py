from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .publish import Publisher


def _managed_audio_path(settings: Settings, value: str, slug: str) -> Path:
    configured_path = Path(value)
    if configured_path.is_symlink():
        raise ValueError(f"Lien symbolique audio refusé : {configured_path}")
    path = configured_path.resolve()
    managed_root = (settings.data_dir / "audio").resolve()
    if path.parent != managed_root:
        raise ValueError(f"Fichier audio hors du répertoire géré : {path}")
    if path.name != f"{slug}.mp3":
        raise ValueError(f"Nom de fichier audio inattendu : {path.name}")
    return path


def delete_episode(settings: Settings, db: Database, episode_id: int) -> dict[str, Any]:
    episode = db.one("SELECT id,slug,title FROM episode WHERE id=?", (episode_id,))
    if not episode:
        raise ValueError(f"Épisode #{episode_id} introuvable")

    audio_rows = db.all("SELECT id,path FROM audio WHERE episode_id=?", (episode_id,))
    audio_paths = [
        _managed_audio_path(settings, row["path"], episode["slug"])
        for row in audio_rows
    ]
    for row in audio_rows:
        shared = db.one(
            "SELECT id FROM audio WHERE path=? AND episode_id<>? LIMIT 1",
            (row["path"], episode_id),
        )
        if shared:
            raise ValueError(f"Fichier audio partagé avec l'audio #{shared['id']}")

    with db.transaction(immediate=True) as connection:
        connection.execute("DELETE FROM feedback WHERE episode_id=?", (episode_id,))
        connection.execute("DELETE FROM airing WHERE episode_id=?", (episode_id,))
        connection.execute("DELETE FROM audio WHERE episode_id=?", (episode_id,))
        connection.execute("DELETE FROM episode WHERE id=?", (episode_id,))
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Suppression annulée : intégrité SQLite invalide")

    removed_audio = 0
    for path in audio_paths:
        if path.is_file():
            path.unlink()
            removed_audio += 1

    published = Publisher(settings, db).publish()
    return {
        "episode_id": episode_id,
        "slug": episode["slug"],
        "title": episode["title"],
        "audio_files": removed_audio,
        "published_episodes": published["episodes"],
    }
