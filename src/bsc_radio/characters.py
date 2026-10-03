from __future__ import annotations

import unicodedata
from typing import Any

DELIVERY_TAGS = {
    "warm": "warmly",
    "cheerful": "cheerfully",
    "excited": "excited",
    "curious": "curious",
    "thoughtful": "thoughtful",
    "serious": "serious",
    "mischievous": "mischievously",
    "sarcastic": "sarcastic",
    "surprised": "surprised",
    "concerned": "concerned",
    "softly": "softly",
    "whispers": "whispers",
    "emphatic": "emphatic",
    "laughs": "laughs",
    "chuckles": "chuckles",
    "sighs": "sighs",
    "clears_throat": "clears throat",
    "short_pause": "short pause",
}


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
    lines: list[dict[str, Any]],
    characters: dict[str, Any],
    allowed_roles: list[str] | None = None,
) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    allowed = [role for role in (allowed_roles or []) if role in characters]
    for line in lines:
        role, _ = resolve_character(characters, line.get("character"))
        if allowed and role not in allowed:
            role = allowed[0]
        normalized = dict(line)
        normalized["character"] = role
        delivery = normalized.get("delivery", [])
        if not isinstance(delivery, list):
            delivery = []
        normalized["delivery"] = [
            str(value) for value in delivery if str(value) in DELIVERY_TAGS
        ][:2]
        for field in ("fact_ids", "feedback_ids"):
            values = normalized.get(field, [])
            normalized[field] = [int(value) for value in values if str(value).isdigit()]
        canonical.append(normalized)
    return canonical


def render_performance_text(text: str, delivery: object) -> str:
    """Add only known Eleven audio tags while keeping factual text untouched."""
    values = delivery if isinstance(delivery, list) else []
    tags: list[str] = []
    for value in values:
        tag = DELIVERY_TAGS.get(str(value))
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) == 2:
            break
    prefix = " ".join(f"[{tag}]" for tag in tags)
    return f"{prefix} {text.strip()}".strip()
