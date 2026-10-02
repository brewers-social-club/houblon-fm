from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

import httpx

from .characters import character_voice, resolve_character
from .config import Settings
from .providers import ElevenLabsVoiceProvider, VoiceProvider


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def _identifier(value: object) -> str:
    identifier = str(value).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", identifier):
        raise ValueError(f"Identifiant d'habillage invalide : {identifier}")
    return identifier


def _normalize(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=9",
            "-b:a",
            "128k",
            str(destination),
        ]
    )


class CreativeAudioProvider(Protocol):
    def sound_effect(
        self, prompt: str, output: Path, duration_seconds: float, *, loop: bool = False
    ) -> None: ...

    def music(self, prompt: str, output: Path, duration_seconds: float) -> None: ...


class ElevenLabsCreativeAudioProvider:
    def __init__(self, settings: Settings):
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY manque")
        self.api_key = settings.elevenlabs_api_key
        antenna = settings.antenna
        self.sound_effect_model = str(
            antenna.get("sound_effect_model", "eleven_text_to_sound_v2")
        )
        self.music_model = str(antenna.get("music_model", "music_v1"))

    def _post(self, endpoint: str, payload: dict[str, Any], output: Path, timeout: int) -> None:
        response = httpx.post(
            f"https://api.elevenlabs.io{endpoint}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(response.content)

    def sound_effect(
        self, prompt: str, output: Path, duration_seconds: float, *, loop: bool = False
    ) -> None:
        self._post(
            "/v1/sound-generation",
            {
                "text": prompt,
                "duration_seconds": max(0.5, min(30.0, float(duration_seconds))),
                "prompt_influence": 0.55,
                "loop": loop,
                "model_id": self.sound_effect_model,
            },
            output,
            180,
        )

    def music(self, prompt: str, output: Path, duration_seconds: float) -> None:
        self._post(
            "/v1/music",
            {
                "prompt": prompt,
                "music_length_ms": int(max(3.0, min(600.0, duration_seconds)) * 1000),
                "model_id": self.music_model,
                "force_instrumental": True,
                "sign_with_c2pa": True,
            },
            output,
            900,
        )


class ImagingProducer:
    def __init__(
        self,
        settings: Settings,
        voice: VoiceProvider | None = None,
        creative: CreativeAudioProvider | None = None,
    ):
        self.settings = settings
        self.voice = voice or ElevenLabsVoiceProvider(settings)
        self.creative = creative or ElevenLabsCreativeAudioProvider(settings)
        self.root = settings.data_dir / "antenna"

    def _destination(self, category: object, identifier: object) -> Path:
        safe_category = _identifier(category)
        safe_identifier = _identifier(identifier)
        return self.root / safe_category / f"{safe_identifier}.mp3"

    def _voice_over(self, speech: Path, bed: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(bed),
                "-i",
                str(speech),
                "-filter_complex",
                "[0:a]volume=0.20[bed];[1:a]adelay=220|220[voice];"
                "[bed][voice]amix=inputs=2:duration=longest:dropout_transition=0,"
                "loudnorm=I=-16:TP=-1.5:LRA=9[out]",
                "-map",
                "[out]",
                "-b:a",
                "128k",
                str(destination),
            ]
        )

    def generate(self, *, force: bool = False) -> dict[str, Any]:
        config = self.settings.antenna
        generated: list[str] = []
        skipped: list[str] = []
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="radio-imaging-", dir=self.root) as directory:
            temp = Path(directory)
            for asset in config.get("sound_effects", []):
                destination = self._destination(asset["category"], asset["id"])
                if destination.exists() and not force:
                    skipped.append(str(destination))
                    continue
                raw = temp / f"{_identifier(asset['id'])}-raw.mp3"
                normalized = temp / f"{_identifier(asset['id'])}-normalized.mp3"
                self.creative.sound_effect(
                    str(asset["prompt"]),
                    raw,
                    float(asset.get("duration_seconds", 3)),
                    loop=bool(asset.get("loop", False)),
                )
                _normalize(raw, normalized)
                destination.parent.mkdir(parents=True, exist_ok=True)
                normalized.replace(destination)
                generated.append(str(destination))

            for asset in config.get("spoken", []):
                destination = self._destination(asset["category"], asset["id"])
                if destination.exists() and not force:
                    skipped.append(str(destination))
                    continue
                role, profile = resolve_character(self.settings.characters, str(asset["character"]))
                speech = temp / f"{_identifier(asset['id'])}-speech.mp3"
                bed = temp / f"{_identifier(asset['id'])}-bed.mp3"
                mixed = temp / f"{_identifier(asset['id'])}-mixed.mp3"
                self.voice.synthesize(
                    str(asset["text"]),
                    character_voice(profile, "elevenlabs", self.settings.elevenlabs_voice_id),
                    speech,
                    profile.get("performance", {}),
                )
                self.creative.sound_effect(
                    str(asset["bed_prompt"]),
                    bed,
                    float(asset.get("bed_duration_seconds", 8)),
                )
                self._voice_over(speech, bed, mixed)
                destination.parent.mkdir(parents=True, exist_ok=True)
                mixed.replace(destination)
                generated.append(str(destination))

            for asset in config.get("music", []):
                destination = self._destination("music", asset["id"])
                if destination.exists() and not force:
                    skipped.append(str(destination))
                    continue
                raw = temp / f"{_identifier(asset['id'])}-music.mp3"
                normalized = temp / f"{_identifier(asset['id'])}-normalized.mp3"
                self.creative.music(
                    str(asset["prompt"]), raw, float(asset.get("duration_seconds", 60))
                )
                _normalize(raw, normalized)
                destination.parent.mkdir(parents=True, exist_ok=True)
                normalized.replace(destination)
                generated.append(str(destination))

        manifest = {
            "generated": generated,
            "skipped": skipped,
            "clock": config.get("clock", []),
        }
        (self.root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
