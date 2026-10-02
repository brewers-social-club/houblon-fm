from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import Settings


class EditorialProvider(Protocol):
    def extract_facts(
        self, title: str, text: str, source: dict[str, str]
    ) -> list[dict[str, Any]]: ...
    def cluster(self, facts: list[dict[str, Any]]) -> dict[str, Any]: ...
    def write(
        self, fmt: str, facts: list[dict[str, Any]], context: dict[str, Any]
    ) -> list[dict[str, Any]]: ...
    def verify(self, sentence: str, facts: list[dict[str, Any]]) -> dict[str, Any]: ...
    def rewrite(self, sentence: str, facts: list[dict[str, Any]]) -> str: ...


class VoiceProvider(Protocol):
    def synthesize(
        self, text: str, voice: str, output: Path, style: str | dict[str, Any] = ""
    ) -> None: ...


@dataclass
class MockEditorialProvider:
    """Provider déterministe pour développement et tests sans coût."""

    def extract_facts(self, title: str, text: str, source: dict[str, str]) -> list[dict[str, Any]]:
        chunks = [
            part.strip() for part in text.replace("\n", " ").split(".") if len(part.strip()) > 25
        ]
        chunks = chunks[:8] or [title]
        return [
            {
                "statement": chunk[:360],
                "type": "context",
                "reliability": 0.7,
                "source_url": source["url"],
                "source_name": source["name"],
                "source_date": source.get("date"),
            }
            for chunk in chunks
        ]

    def cluster(self, facts: list[dict[str, Any]]) -> dict[str, Any]:
        statement = facts[0]["statement"] if facts else "Actualité brassicole"
        return {
            "title": statement[:80],
            "summary": " ".join(f["statement"] for f in facts[:3])[:400],
            "station": "brasserie",
            "event": None,
        }

    def write(
        self, fmt: str, facts: list[dict[str, Any]], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        disclosure = context["disclosure"]
        script = [{"character": "presenter", "text": disclosure, "fact_ids": []}]
        if not facts:
            return script + [
                {
                    "character": "presenter",
                    "text": "Aucune actualité vérifiée à signaler.",
                    "fact_ids": [],
                }
            ]
        for fact in facts[:6]:
            source = fact["source_name"]
            script.append(
                {
                    "character": "presenter",
                    "text": f"Selon {source}, {fact['statement'].rstrip('.')}.",
                    "fact_ids": [fact["id"]],
                }
            )
        script.append({"character": "presenter", "text": context["health_notice"], "fact_ids": []})
        return script

    def verify(self, sentence: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
        supported = not facts or any(
            token.lower().strip(",.") in " ".join(f["statement"] for f in facts).lower()
            for token in sentence.split()
            if len(token) > 7
        )
        return {"verdict": "supported" if supported else "unsupported", "flags": [], "note": "mock"}

    def rewrite(self, sentence: str, facts: list[dict[str, Any]]) -> str:
        return facts[0]["statement"] if facts else ""


class AnthropicEditorialProvider:
    def __init__(self, settings: Settings):
        from anthropic import Anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY manque")
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.light = settings.light_model
        self.write_model = settings.write_model
        self.prompts_dir = settings.root / "prompts"

    def _prompt(self, name: str, fallback: str) -> str:
        path = self.prompts_dir / f"{name}.md"
        return path.read_text(encoding="utf-8") if path.exists() else fallback

    def _json(self, model: str, system: str, payload: dict[str, Any]) -> Any:
        response = self.client.messages.create(
            model=model,
            max_tokens=6000,
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())

    def extract_facts(self, title: str, text: str, source: dict[str, str]) -> list[dict[str, Any]]:
        return self._json(
            self.light,
            self._prompt("extract_facts", "Extrais des faits atomiques reformulés en JSON strict."),
            {"title": title, "text": text, "source": source},
        )

    def cluster(self, facts: list[dict[str, Any]]) -> dict[str, Any]:
        return self._json(
            self.light,
            self._prompt("cluster", "Résume ce groupe de faits en sujet brassicole JSON."),
            {"facts": facts},
        )

    def write(
        self, fmt: str, facts: list[dict[str, Any]], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self._json(
            self.write_model,
            self._prompt("common", "Écris uniquement depuis les faits.")
            + "\n"
            + self._prompt(f"write_{fmt}", "Écris une émission orale en JSON strict."),
            {"format": fmt, "facts": facts, "context": context},
        )

    def verify(self, sentence: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
        return self._json(
            self.light,
            self._prompt("verify", "Vérifie strictement chaque phrase contre ses faits en JSON."),
            {"sentence": sentence, "facts": facts},
        )

    def rewrite(self, sentence: str, facts: list[dict[str, Any]]) -> str:
        result = self._json(
            self.write_model,
            "Réécris la phrase pour qu'elle soit entièrement soutenue. JSON: {text}.",
            {"sentence": sentence, "facts": facts},
        )
        return str(result.get("text", ""))


class MockVoiceProvider:
    def synthesize(self, text: str, voice: str, output: Path, style: str = "") -> None:
        duration = max(1.2, min(25.0, len(text.split()) / 2.6))
        sample_rate = 24000
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temp:
            wav = Path(temp) / "voice.wav"
            with wave.open(str(wav), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                for index in range(int(sample_rate * duration)):
                    envelope = min(1.0, index / 800, (sample_rate * duration - index) / 800)
                    value = int(1800 * envelope * math.sin(2 * math.pi * 180 * index / sample_rate))
                    handle.writeframesraw(struct.pack("<h", value))
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-b:a", "128k", str(output)],
                check=True,
            )


class GeminiVoiceProvider:
    def __init__(self, settings: Settings):
        from google import genai

        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY manque")
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = settings.tts_model

    def synthesize(self, text: str, voice: str, output: Path, style: str = "") -> None:
        from google.genai import types

        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{style}\n{text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                    )
                ),
            ),
        )
        inline_data = response.candidates[0].content.parts[0].inline_data
        if not inline_data or not inline_data.data:
            raise RuntimeError("Gemini TTS n'a renvoyé aucun audio")
        data = inline_data.data
        mime_type = (inline_data.mime_type or "").lower()
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="gemini-tts-") as directory:
            source = Path(directory) / "speech.wav"
            if data[:4] == b"RIFF" or "audio/wav" in mime_type:
                source.write_bytes(data)
            else:
                rate_match = re.search(r"rate=(\d+)", mime_type)
                sample_rate = int(rate_match.group(1)) if rate_match else 24000
                with wave.open(str(source), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(sample_rate)
                    handle.writeframes(data)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-b:a",
                    "128k",
                    str(output),
                ],
                check=True,
            )


class ElevenLabsVoiceProvider:
    def __init__(self, settings: Settings):
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY manque")
        self.api_key = settings.elevenlabs_api_key
        self.voice_id = settings.elevenlabs_voice_id
        self.model = settings.elevenlabs_model
        self.style = settings.elevenlabs_style

    def synthesize(
        self, text: str, voice: str, output: Path, style: str | dict[str, Any] = ""
    ) -> None:
        voice_id = voice.strip() or self.voice_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
            raise ValueError("Identifiant de voix ElevenLabs invalide")
        voice_settings: dict[str, float | bool] = {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": self.style,
            "speed": 1.0,
            "use_speaker_boost": True,
        }
        if isinstance(style, dict):
            allowed = {"stability", "similarity_boost", "style", "speed", "use_speaker_boost"}
            voice_settings.update({key: value for key, value in style.items() if key in allowed})
        response = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": self.model,
                "voice_settings": voice_settings,
            },
            timeout=120,
        )
        response.raise_for_status()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(response.content)


def editorial_provider(settings: Settings) -> EditorialProvider:
    if settings.ai_provider == "codex":
        from .codex_provider import CodexEditorialProvider

        return CodexEditorialProvider(settings)
    if settings.ai_provider == "anthropic":
        return AnthropicEditorialProvider(settings)
    if settings.ai_provider == "mock":
        return MockEditorialProvider()
    raise RuntimeError(f"Provider éditorial inconnu : {settings.ai_provider}")


def voice_provider(settings: Settings) -> VoiceProvider:
    if settings.tts_provider == "elevenlabs":
        return ElevenLabsVoiceProvider(settings)
    if settings.tts_provider == "gemini":
        return GeminiVoiceProvider(settings)
    if settings.tts_provider == "mock":
        return MockVoiceProvider()
    raise RuntimeError(f"Provider vocal inconnu : {settings.tts_provider}")


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
