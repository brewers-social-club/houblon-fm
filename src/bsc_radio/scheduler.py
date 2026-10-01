from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database


@dataclass
class ScheduledTrack:
    episode_id: int
    audio_id: int
    path: str
    title: str
    format: str
    reason: str


class Scheduler:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def _fixed_format(self, now: dt.datetime) -> str | None:
        local = now.astimezone(ZoneInfo(self.settings.radio.get("timezone", "Europe/Paris")))
        for slot in self.settings.schedule.get("fixed_slots", []):
            if "weekday" in slot and int(slot["weekday"]) != local.weekday():
                continue
            hour, minute = (int(value) for value in slot["time"].split(":"))
            target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if abs((local - target).total_seconds()) <= 15 * 60:
                aired = self.db.one(
                    """SELECT 1 FROM airing a JOIN episode e ON e.id=a.episode_id
                    WHERE e.format=? AND a.started_at>=? LIMIT 1""",
                    (slot["format"], target.isoformat()),
                )
                if not aired:
                    return str(slot["format"])
        return None

    def next(self, now: dt.datetime | None = None) -> ScheduledTrack | None:
        now = now or dt.datetime.now(dt.UTC)
        fixed = self._fixed_format(now)
        last = self.db.one(
            """SELECT e.id episode_id,e.format FROM airing a JOIN episode e ON e.id=a.episode_id
            ORDER BY a.started_at DESC LIMIT 1"""
        )
        rows = self.db.all(
            """SELECT e.id episode_id,e.title,e.format,a.id audio_id,a.path,
            MAX(ai.started_at) last_airing FROM episode e JOIN audio a ON a.episode_id=e.id
            LEFT JOIN airing ai ON ai.episode_id=e.id
            WHERE e.status IN ('audio','published') AND e.hidden=0
            GROUP BY e.id,a.id ORDER BY last_airing IS NOT NULL,last_airing,e.created_at"""
        )
        candidates = [row for row in rows if not last or row["episode_id"] != last["episode_id"]]
        if fixed:
            candidates = [row for row in candidates if row["format"] == fixed] or candidates
            reason = f"créneau fixe {fixed}"
        else:
            different = [row for row in candidates if not last or row["format"] != last["format"]]
            candidates = different or candidates
            reason = "moins récemment diffusée"
        if not candidates:
            return None
        row = candidates[0]
        return ScheduledTrack(
            episode_id=row["episode_id"],
            audio_id=row["audio_id"],
            path=row["path"],
            title=row["title"],
            format=row["format"],
            reason=reason,
        )

    def mark_started(self, track: ScheduledTrack) -> int:
        return self.db.execute(
            "INSERT INTO airing(episode_id,audio_id,started_at,reason) VALUES(?,?,?,?)",
            (track.episode_id, track.audio_id, dt.datetime.now(dt.UTC).isoformat(), track.reason),
        )
