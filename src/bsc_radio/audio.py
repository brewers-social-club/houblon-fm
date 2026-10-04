from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from .characters import (
    canonicalize_script,
    character_voice,
    render_performance_text,
    resolve_character,
)
from .config import Settings
from .db import Database
from .providers import VoiceProvider

LOGGER = logging.getLogger(__name__)
DIALOGUE_CHARACTER_LIMIT = 1900


def group_script_takes(lines: list[dict[str, object]]) -> list[dict[str, str]]:
    """Keep one TTS take per contiguous character block."""
    takes: list[dict[str, str]] = []
    for line in lines:
        character = str(line["character"])
        text = str(line["text"]).strip()
        if takes and takes[-1]["character"] == character:
            takes[-1]["text"] += f"\n\n{text}"
        else:
            takes.append({"character": character, "text": text})
    return takes


def _split_long_text(text: str, limit: int) -> list[str]:
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        positions = [window.rfind(separator) for separator in (". ", "! ", "? ", "; ", ", ", " ")]
        cut = max(positions)
        if cut < limit // 2:
            cut = limit
        elif window[cut : cut + 2] in {". ", "! ", "? ", "; ", ", "}:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def split_dialogue_chunks(
    inputs: list[dict[str, object]], limit: int = DIALOGUE_CHARACTER_LIMIT
) -> list[list[dict[str, object]]]:
    """Keep complete turns when possible and stay below ElevenLabs' hard limit."""
    expanded: list[dict[str, object]] = []
    for item in inputs:
        for text in _split_long_text(str(item["text"]), limit):
            expanded.append({**item, "text": text})
    chunks: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    size = 0
    for item in expanded:
        length = len(str(item["text"]))
        if current and size + length > limit:
            chunks.append(current)
            current = []
            size = 0
        current.append(item)
        size += length
    if current:
        chunks.append(current)
    return chunks


def validate_spoken_audio(path: Path, text: str) -> float:
    if not path.is_file() or path.stat().st_size < 512:
        raise RuntimeError("ElevenLabs a renvoyé un fichier audio vide ou incomplet")
    seconds = duration(path)
    words = len(re.findall(r"\b[\wÀ-ÿ'-]+\b", re.sub(r"\[[^]]+\]", "", text)))
    minimum = max(0.6, words / 6.5)
    if seconds < minimum:
        raise RuntimeError(
            f"Audio probablement tronqué : {seconds:.1f}s pour {words} mots (minimum {minimum:.1f}s)"
        )
    return seconds


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def apply_tempo(path: Path, tempo: float) -> None:
    """Accelerate a generated take without asking Eleven v4 for an unsupported speed setting."""
    tempo = max(0.7, min(1.2, float(tempo)))
    if abs(tempo - 1.0) < 0.005:
        return
    accelerated = path.with_name(f"{path.stem}-tempo{path.suffix}")
    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-filter:a",
            f"atempo={tempo:.3f}",
            "-b:a",
            "128k",
            str(accelerated),
        ]
    )
    accelerated.replace(path)


def dialogue_tempo(inputs: list[dict[str, object]]) -> float:
    weighted = [
        (len(str(item.get("text", ""))), float(item.get("tempo", 1.0))) for item in inputs
    ]
    total = sum(length for length, _ in weighted)
    return sum(length * tempo for length, tempo in weighted) / total if total else 1.0


def duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def make_tone(output: Path, seconds: float = 1.2, frequency: int = 523) -> Path:
    """Generate a rights-free station tone with an atomic publication."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="radio-tone-", dir=output.parent) as directory:
        temporary = Path(directory) / output.name
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
                str(temporary),
            ]
        )
        temporary.replace(output)
    return output


def ensure_fallback_audio(output: Path) -> Path:
    if output.is_file() and output.stat().st_size > 0:
        return output
    return make_tone(output, seconds=15, frequency=220)


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

    def _dialogue_inputs(self, lines: list[dict[str, object]]) -> list[dict[str, object]]:
        inputs: list[dict[str, object]] = []
        for line in lines:
            _, character = resolve_character(self.settings.characters, line["character"])
            text = render_performance_text(
                self._pronounce(str(line["text"])), line.get("delivery", [])
            )
            inputs.append(
                {
                    "voice_id": character_voice(
                        character, "elevenlabs", self.settings.elevenlabs_voice_id
                    ),
                    "text": text,
                    "tempo": float(character.get("performance", {}).get("speed", 1.0)),
                }
            )
        return inputs

    def _dialogue_parts(
        self, lines: list[dict[str, object]], temp: Path
    ) -> list[Path] | None:
        synthesize_dialogue = getattr(self.voice, "synthesize_dialogue", None)
        if not callable(synthesize_dialogue) or not getattr(self.voice, "supports_dialogue", False):
            return None
        chunks = split_dialogue_chunks(self._dialogue_inputs(lines))
        parts: list[Path] = []
        for index, chunk in enumerate(chunks):
            part = temp / f"dialogue-{index:03}.mp3"
            previous_text = (
                chunks[index - 1][-1]["text"][-100:] if index > 0 else ""
            )
            future_text = (
                chunks[index + 1][0]["text"][:100] if index + 1 < len(chunks) else ""
            )
            synthesize_dialogue(
                chunk,
                part,
                previous_text=previous_text,
                future_text=future_text,
            )
            apply_tempo(part, dialogue_tempo(chunk))
            validate_spoken_audio(part, " ".join(str(item["text"]) for item in chunk))
            parts.append(part)
        return parts

    def _sequential_parts(self, lines: list[dict[str, object]], temp: Path) -> list[Path]:
        expressive = self.settings.tts_provider == "elevenlabs" and self.settings.elevenlabs_model.startswith(
            ("eleven_v3", "eleven_v4")
        )
        needs_tempo_postprocess = (
            self.settings.tts_provider == "elevenlabs"
            and self.settings.elevenlabs_model.startswith("eleven_v4")
        )
        prepared = [
            {
                **line,
                "text": render_performance_text(str(line["text"]), line.get("delivery", []))
                if expressive
                else str(line["text"]),
            }
            for line in lines
        ]
        parts: list[Path] = []
        for index, take in enumerate(group_script_takes(prepared)):
            _, character = resolve_character(self.settings.characters, take["character"])
            part = temp / f"take-{index:03}.mp3"
            spoken = self._pronounce(take["text"])
            self.voice.synthesize(
                spoken,
                character_voice(
                    character,
                    self.settings.tts_provider,
                    self.settings.elevenlabs_voice_id
                    if self.settings.tts_provider == "elevenlabs"
                    else "",
                ),
                part,
                character.get("performance", {})
                if self.settings.tts_provider == "elevenlabs"
                else (
                    "Français naturel, vivant sans excès. "
                    f"Personnage fictif: {character['description']}"
                ),
            )
            if needs_tempo_postprocess:
                apply_tempo(part, float(character.get("performance", {}).get("speed", 1.0)))
            validate_spoken_audio(part, spoken)
            parts.append(part)
        return parts

    def produce(self, episode_id: int) -> Path:
        episode = self.db.one("SELECT * FROM episode WHERE id=?", (episode_id,))
        if not episode or episode["status"] not in {"verified", "audio", "published"}:
            raise ValueError("L'épisode doit être vérifié avant vocalisation")
        lines = canonicalize_script(json.loads(episode["script_json"]), self.settings.characters)
        destination = self.settings.data_dir / "audio" / f"{episode['slug']}.mp3"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="radio-episode-", dir=destination.parent
        ) as temp_dir:
            temp = Path(temp_dir)
            try:
                parts = self._dialogue_parts(lines, temp)
            except Exception as exc:
                LOGGER.warning(
                    "Text-to-Dialogue indisponible (%s), repli sur les prises séparées",
                    exc,
                )
                parts = None
            if not parts:
                parts = self._sequential_parts(lines, temp)
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
            mixed.replace(destination)
        seconds = duration(destination)
        checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
        with self.db.transaction() as connection:
            updated = connection.execute(
                """UPDATE audio
                SET path=?,duration_seconds=?,bitrate=128,lufs=?,checksum=?,created_at=CURRENT_TIMESTAMP
                WHERE episode_id=?""",
                (str(destination), seconds, -16.0, checksum, episode_id),
            )
            if updated.rowcount == 0:
                connection.execute(
                    """INSERT INTO audio(episode_id,path,duration_seconds,lufs,checksum)
                    VALUES(?,?,?,?,?)""",
                    (episode_id, str(destination), seconds, -16.0, checksum),
                )
            connection.execute(
                "UPDATE episode SET status='audio',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (episode_id,),
            )
        return destination

    def make_jingle(self, output: Path, seconds: float = 1.2, frequency: int = 523) -> Path:
        return make_tone(output, seconds=seconds, frequency=frequency)
