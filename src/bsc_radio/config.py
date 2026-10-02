from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - message d'installation
        raise RuntimeError("PyYAML est requis. Lancez: uv sync") from exc
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    config_dir: Path
    site_dir: Path
    database: Path
    site_url: str
    user_agent: str
    ai_provider: str
    tts_provider: str
    admin_login: str
    admin_password: str
    session_secret: str
    anthropic_api_key: str
    gemini_api_key: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    elevenlabs_style: float
    codex_bin: str
    codex_light_model: str
    codex_write_model: str
    codex_timeout_seconds: int
    write_model: str
    light_model: str
    tts_model: str
    embed_model: str
    video_model: str
    tts_daily_limit: int
    budget_monthly_eur: float

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        root = (root or Path.cwd()).resolve()
        load_dotenv(root / ".env", override=False)
        data = Path(os.getenv("RADIO_DATA_DIR", root / "data")).resolve()
        config = Path(os.getenv("RADIO_CONFIG_DIR", root / "config")).resolve()
        site = Path(os.getenv("RADIO_SITE_DIR", root / "site/public")).resolve()
        codex_default_model = os.getenv("CODEX_MODEL", "").strip()
        return cls(
            root=root,
            data_dir=data,
            config_dir=config,
            site_dir=site,
            database=data / "radio.sqlite3",
            site_url=os.getenv("SITE_URL", "http://localhost:8080").rstrip("/"),
            user_agent=os.getenv("USER_AGENT", "BscRadioBot/0.1 (+https://example.test)"),
            ai_provider=os.getenv("RADIO_AI_PROVIDER", "mock"),
            tts_provider=os.getenv("RADIO_TTS_PROVIDER", "mock"),
            admin_login=os.getenv("RADIO_ADMIN_LOGIN", "admin").strip() or "admin",
            admin_password=os.getenv("RADIO_ADMIN_PASSWORD", "change-me"),
            session_secret=os.getenv("RADIO_SESSION_SECRET", "development-only"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
            elevenlabs_model=os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
            elevenlabs_style=float(os.getenv("ELEVENLABS_STYLE", "0.6")),
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_light_model=os.getenv("CODEX_MODEL_LIGHT", "").strip() or codex_default_model,
            codex_write_model=os.getenv("CODEX_MODEL_WRITE", "").strip() or codex_default_model,
            codex_timeout_seconds=int(os.getenv("CODEX_TIMEOUT_SECONDS", "300")),
            write_model=os.getenv("ANTHROPIC_MODEL_WRITE", "claude-opus-4-1"),
            light_model=os.getenv("ANTHROPIC_MODEL_LIGHT", "claude-haiku-4-5"),
            tts_model=os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
            embed_model=os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001"),
            video_model=os.getenv("GEMINI_VIDEO_MODEL", "gemini-2.5-flash"),
            tts_daily_limit=int(os.getenv("GEMINI_TTS_DAILY_LIMIT", "100")),
            budget_monthly_eur=float(os.getenv("BUDGET_MENSUEL_EUR", "25")),
        )

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.site_dir, self.data_dir / "audio", self.data_dir / "raw"):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def radio(self) -> dict[str, Any]:
        return _yaml(self.config_dir / "radio.yaml")

    @property
    def schedule(self) -> dict[str, Any]:
        return _yaml(self.config_dir / "schedule.yaml")

    @property
    def characters(self) -> dict[str, Any]:
        return _yaml(self.config_dir / "characters.yaml")

    @property
    def sources(self) -> list[dict[str, Any]]:
        configured = list(_yaml(self.config_dir / "sources.yaml").get("sources", []))
        fragments = self.config_dir / "sources.d"
        if fragments.is_dir():
            for path in sorted(fragments.glob("*.yaml")):
                configured.extend(_yaml(path).get("sources", []))
        merged: dict[str, dict[str, Any]] = {}
        for source in configured:
            identifier = str(source["id"])
            merged[identifier] = {**merged.get(identifier, {}), **source}
        return list(merged.values())

    @property
    def lexicon(self) -> dict[str, str]:
        return _yaml(self.config_dir / "lexicon.yaml")
