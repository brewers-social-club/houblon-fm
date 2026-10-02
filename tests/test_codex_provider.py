from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bsc_radio.codex_provider import CodexEditorialProvider
from bsc_radio.config import Settings
from bsc_radio.providers import editorial_provider
from tests.helpers import settings


class FakeCodex:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def __call__(self, command, **kwargs):
        if command[1:] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in", "")
        schema_path = Path(command[command.index("--output-schema") + 1])
        output_path = Path(command[command.index("--output-last-message") + 1])
        title = json.loads(schema_path.read_text(encoding="utf-8"))["title"]
        outputs = {
            "facts": {
                "facts": [
                    {
                        "statement": "La fermentation a duré cinq jours.",
                        "type": "result",
                        "reliability": 0.9,
                        "source_url": "https://source.test/article",
                        "source_name": "Source",
                        "source_date": "2026-10-02",
                        "quote": None,
                    }
                ]
            },
            "topic": {
                "title": "Fermentation stable",
                "summary": "Une fermentation suivie pendant cinq jours.",
                "station": "brassage",
                "event": None,
            },
            "script": {
                "lines": [
                    {"character": "presenter", "text": "Texte vérifié.", "fact_ids": [1]}
                ]
            },
            "verdict": {"verdict": "supported", "flags": [], "note": "Soutenu."},
            "rewrite": {"text": "La fermentation a duré cinq jours."},
        }
        output_path.write_text(json.dumps(outputs[title]), encoding="utf-8")
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, "", "")


def test_codex_provider_uses_stdin_sandbox_and_structured_outputs(tmp_path: Path):
    cfg = settings(tmp_path)
    fake = FakeCodex()
    provider = CodexEditorialProvider(cfg, runner=fake)

    facts = provider.extract_facts(
        "Fermentation",
        "Un texte source qui ne doit jamais apparaître dans la ligne de commande.",
        {"name": "Source", "url": "https://source.test/article", "date": "2026-10-02"},
    )
    topic = provider.cluster([{"id": 1, **facts[0]}])
    script = provider.write("journal", [{"id": 1, **facts[0]}], {"date": "now"})
    verdict = provider.verify(script[0]["text"], [{"id": 1, **facts[0]}])
    rewrite = provider.rewrite("Phrase fausse", [{"id": 1, **facts[0]}])

    assert facts[0]["reliability"] == 0.9
    assert topic["station"] == "brassage"
    assert script[0]["fact_ids"] == [1]
    assert verdict["verdict"] == "supported"
    assert rewrite == "La fermentation a duré cinq jours."
    assert len(fake.calls) == 5
    expected_models = [
        "gpt-light-test",
        "gpt-light-test",
        "gpt-write-test",
        "gpt-light-test",
        "gpt-write-test",
    ]
    for call, expected_model in zip(fake.calls, expected_models, strict=True):
        command = call["command"]
        assert command[-1] == "-"
        assert "--ephemeral" in command
        assert command[
            command.index("--sandbox") : command.index("--sandbox") + 2
        ] == ["--sandbox", "read-only"]
        assert "Un texte source" not in " ".join(command)
        assert "ENTRÉE JSON" in call["input"]
        assert call["capture_output"] is True
        assert call["env"].keys() <= {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL"}
        assert "shell_environment_policy.inherit=none" in command
        assert command[command.index("--model") + 1] == expected_model
        for feature in (
            "shell_tool",
            "unified_exec",
            "apps",
            "plugins",
            "multi_agent",
            "browser_use",
            "computer_use",
            "hooks",
        ):
            assert feature in command


def test_codex_provider_reports_missing_binary_without_leaking_secret(tmp_path: Path):
    cfg = settings(tmp_path)

    def missing(*args, **kwargs):
        raise FileNotFoundError

    with pytest.raises(RuntimeError, match="Codex CLI introuvable"):
        CodexEditorialProvider(cfg, runner=missing).cluster([])


def test_codex_provider_redacts_credentials_from_failures(tmp_path: Path):
    cfg = settings(tmp_path)

    def failure(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "token sk-exampleVerySecret123456")

    with pytest.raises(RuntimeError) as error:
        CodexEditorialProvider(cfg, runner=failure).cluster([])
    assert "exampleVerySecret" not in str(error.value)
    assert "[secret redacted]" in str(error.value)


def test_factory_selects_codex(tmp_path: Path):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "ai_provider", "codex")
    assert isinstance(editorial_provider(cfg), CodexEditorialProvider)


def test_legacy_codex_model_is_used_as_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "legacy-model")
    monkeypatch.delenv("CODEX_MODEL_LIGHT", raising=False)
    monkeypatch.delenv("CODEX_MODEL_WRITE", raising=False)
    cfg = Settings.load(tmp_path)
    assert cfg.codex_light_model == "legacy-model"
    assert cfg.codex_write_model == "legacy-model"


def test_specific_codex_models_override_legacy_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "legacy-model")
    monkeypatch.setenv("CODEX_MODEL_LIGHT", "light-model")
    monkeypatch.setenv("CODEX_MODEL_WRITE", "write-model")
    cfg = Settings.load(tmp_path)
    assert cfg.codex_light_model == "light-model"
    assert cfg.codex_write_model == "write-model"


def test_codex_omits_model_flag_when_no_model_is_configured(tmp_path: Path):
    cfg = settings(tmp_path)
    object.__setattr__(cfg, "codex_light_model", "")
    object.__setattr__(cfg, "codex_write_model", "")
    fake = FakeCodex()
    CodexEditorialProvider(cfg, runner=fake).cluster([])
    assert "--model" not in fake.calls[0]["command"]
