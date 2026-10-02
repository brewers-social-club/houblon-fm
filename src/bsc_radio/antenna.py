from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .scheduler import ScheduledTrack, Scheduler


@dataclass
class AntennaTrack:
    path: str
    title: str
    kind: str
    episode: ScheduledTrack | None = None


class AntennaClock:
    def __init__(self, settings: Settings, db: Database, scheduler: Scheduler | None = None):
        self.settings = settings
        self.db = db
        self.scheduler = scheduler or Scheduler(settings, db)

    def _integer(self, key: str) -> int:
        row = self.db.one("SELECT value FROM kv WHERE key=?", (key,))
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _set(self, key: str, value: object) -> None:
        self.db.execute(
            """INSERT INTO kv(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
            (key, str(value)),
        )

    def _episode(self) -> AntennaTrack | None:
        episode = self.scheduler.next()
        if not episode:
            return None
        return AntennaTrack(
            path=episode.path,
            title=episode.title,
            kind="episode",
            episode=episode,
        )

    def _asset_title(self, category: str, path: Path) -> str:
        config = self.settings.antenna
        collections: list[dict[str, Any]] = []
        collections.extend(config.get("sound_effects", []))
        collections.extend(config.get("spoken", []))
        collections.extend(config.get("music", []))
        for asset in collections:
            if str(asset.get("id")) == path.stem:
                return str(asset.get("title") or asset.get("text") or path.stem)
        return path.stem.replace("-", " ").title()

    def _asset(self, category: str) -> AntennaTrack | None:
        directory = self.settings.data_dir / "antenna" / category
        paths = sorted(path for path in directory.glob("*.mp3") if path.is_file())
        if not paths:
            return None
        key = f"antenna.asset.{category}"
        index = self._integer(key) % len(paths)
        self._set(key, index + 1)
        path = paths[index]
        return AntennaTrack(
            path=str(path),
            title=self._asset_title(category, path),
            kind=category,
        )

    def next(self) -> AntennaTrack | None:
        config = self.settings.antenna
        if not config.get("enabled", True):
            return self._episode()
        clock = [str(value) for value in config.get("clock", ["episode"])] or ["episode"]
        start = self._integer("antenna.clock.index")
        for offset in range(len(clock)):
            index = (start + offset) % len(clock)
            category = clock[index]
            self._set("antenna.clock.index", index + 1)
            track = self._episode() if category == "episode" else self._asset(category)
            if track:
                return track
        return self._episode()

    def mark_started(self, track: AntennaTrack) -> None:
        if track.episode:
            self.scheduler.mark_started(track.episode)
        payload = {
            "title": track.title,
            "format": track.episode.format if track.episode else track.kind,
            "kind": track.kind,
            "slug": None,
        }
        if track.episode:
            row = self.db.one("SELECT slug FROM episode WHERE id=?", (track.episode.episode_id,))
            payload["slug"] = row["slug"] if row else None
        self._set("antenna.now", json.dumps(payload, ensure_ascii=False))

    def now(self) -> dict[str, Any] | None:
        row = self.db.one("SELECT value,updated_at FROM kv WHERE key='antenna.now'")
        if not row:
            return None
        try:
            payload = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return None
        payload["started_at"] = row["updated_at"]
        return payload
