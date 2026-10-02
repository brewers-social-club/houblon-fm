from __future__ import annotations

import unicodedata
from typing import Any


def _character_token(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def resolve_character(characters: dict[str, Any], value: object) -> tuple[str, dict[str, Any]]:
    """Resolve a role key, display name or alias to its canonical profile."""
    if not characters or "presenter" not in characters:
        raise ValueError("Le profil de personnage 'presenter' est obligatoire")

    wanted = _character_token(value)
    for role, raw_profile in characters.items():
        profile = dict(raw_profile or {})
        aliases = profile.get("aliases", [])
        candidates = [role, profile.get("name", "")]
        if isinstance(aliases, list):
            candidates.extend(aliases)
        if wanted and any(_character_token(candidate) == wanted for candidate in candidates):
            return role, profile

    return "presenter", dict(characters["presenter"] or {})


def character_voice(profile: dict[str, Any], provider: str, fallback: str = "") -> str:
    """Return the provider-specific voice while accepting legacy scalar configs."""
    configured = profile.get("voice", "")
    if isinstance(configured, dict):
        selected = configured.get(provider) or configured.get("default") or ""
    else:
        selected = configured
    return str(selected or fallback).strip()


def canonicalize_script(
    lines: list[dict[str, Any]], characters: dict[str, Any]
) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for line in lines:
        role, _ = resolve_character(characters, line.get("character"))
        normalized = dict(line)
        normalized["character"] = role
        canonical.append(normalized)
    return canonical
