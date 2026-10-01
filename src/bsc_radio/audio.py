from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Settings
from .db import Database
from .providers import VoiceProvider


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


class AudioProducer:
    def __init__(self, settings: Settings, db: Database, voice: VoiceProvider):
        self.settings = settings
        self.db = db
        self.voice = voice

    def _pronounce(self, text: str) -> str:
        lexicon = dict(self.settings.lexicon)
        lexicon.update(
            {
                row["term"]: row["pronunciation"]
                for row in self.db.all("SELECT * FROM lexicon_entry")
            }
        )
        for term in sorted(lexicon, key=len, reverse=True):
            text = text.replace(term, lexicon[term])
        return text

    def produce(self, episode_id: int) -> Path:
        episode = self.db.one("SELECT * FROM episode WHERE id=?", (episode_id,))
        if not episode or episode["status"] not in {"verified", "audio", "published"}:
            raise ValueError("L'épisode doit être vérifié avant vocalisation")
        lines = json.loads(episode["script_json"])
        characters = self.settings.characters
        destination = self.settings.data_dir / "audio" / f"{episode['slug']}.mp3"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            parts: list[Path] = []
            for index, line in enumerate(lines):
                character = characters.get(line["character"], characters["presenter"])
                part = temp / f"{index:03}.mp3"
                self.voice.synthesize(
                    self._pronounce(line["text"]),
                    character["voice"],
                    part,
                    f"Français naturel, vivant sans excès. Personnage fictif: {character['description']}",
                )
                parts.append(part)
            concat = temp / "concat.txt"
            concat.write_text("".join(f"file '{part}'\n" for part in parts), encoding="utf-8")
            mixed = temp / "mixed.mp3"
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat),
                    "-af",
                    "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-b:a",
                    "128k",
                    "-metadata",
                    f"title={episode['title']}",
                    "-metadata",
                    "comment=Émission écrite et lue par des intelligences artificielles",
                    str(mixed),
                ]
            )
            shutil.copy2(mixed, destination)
        seconds = duration(destination)
        checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM audio WHERE episode_id=?", (episode_id,))
            connection.execute(
                "INSERT INTO audio(episode_id,path,duration_seconds,lufs,checksum) VALUES(?,?,?,?,?)",
                (episode_id, str(destination), seconds, -16.0, checksum),
            )
            connection.execute(
                "UPDATE episode SET status='audio',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (episode_id,),
            )
        return destination

    def make_jingle(self, output: Path, seconds: float = 1.2, frequency: int = 523) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration={seconds}",
                "-af",
                "afade=t=in:st=0:d=0.15,afade=t=out:st=0.9:d=0.3,loudnorm=I=-16",
                "-b:a",
                "128k",
                str(output),
            ]
        )
        return output
