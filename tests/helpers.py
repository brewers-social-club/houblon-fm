from __future__ import annotations

from pathlib import Path

from bsc_radio.config import Settings


def settings(tmp: Path) -> Settings:
    root = Path(__file__).parents[1]
    return Settings(
        root=root,
        data_dir=tmp / "data",
        config_dir=root / "config",
        site_dir=tmp / "site",
        database=tmp / "data/radio.sqlite3",
        site_url="http://test",
        user_agent="RadioTest/1",
        ai_provider="mock",
        tts_provider="mock",
        admin_login="admin",
        admin_password="admin",
        session_secret="secret",
        anthropic_api_key="",
        gemini_api_key="",
        elevenlabs_api_key="",
        elevenlabs_voice_id="test-voice",
        elevenlabs_model="test-model",
        elevenlabs_style=0.6,
        codex_bin="codex",
        codex_light_model="gpt-light-test",
        codex_write_model="gpt-write-test",
        codex_timeout_seconds=300,
        write_model="write",
        light_model="light",
        tts_model="tts",
        embed_model="embed",
        video_model="video",
        tts_daily_limit=100,
        budget_monthly_eur=25.0,
    )
