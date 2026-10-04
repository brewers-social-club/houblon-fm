from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .config import Settings

CONFIG_FILES = {
    "radio.yaml": ("Identité de la radio", "Identité, mentions et règles générales"),
    "schedule.yaml": ("Formats et grille", "Formats, durées, casting et créneaux fixes"),
    "characters.yaml": ("Personnages et voix", "Casting, voix et réglages d’interprétation"),
    "antenna.yaml": ("Habillage d’antenne", "Horloge, jingles, interventions et musiques"),
    "sources.yaml": ("Sources principales", "Flux éditoriaux collectés par la radio"),
    "video_series.yaml": ("Séries vidéo", "Chaînes et séries vidéo suivies"),
    "lexicon.yaml": ("Prononciation", "Lexique appliqué avant la synthèse vocale"),
}
CONFIG_WRITE_LOCK = threading.Lock()
MAX_CONFIG_BYTES = 512_000


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _managed_paths(config_dir: Path) -> dict[str, Path]:
    managed = {
        name: config_dir / name
        for name in CONFIG_FILES
        if (config_dir / name).is_file() and not (config_dir / name).is_symlink()
    }
    fragments = config_dir / "sources.d"
    if fragments.is_dir():
        for path in sorted(fragments.glob("*.yaml")):
            if path.is_file() and not path.is_symlink():
                managed[f"sources.d/{path.name}"] = path
    return managed


def _metadata(name: str, path: Path) -> dict[str, Any]:
    stat = path.stat()
    if name in CONFIG_FILES:
        label, description = CONFIG_FILES[name]
        group = "Configuration"
    else:
        label = path.stem.replace("-", " ").replace("_", " ").title()
        description = "Sources complémentaires"
        group = "Sources complémentaires"
    return {
        "name": name,
        "label": label,
        "description": description,
        "group": group,
        "bytes": stat.st_size,
        "updated_at": stat.st_mtime,
        "writable": os.access(path, os.W_OK),
    }


def list_config_files(settings: Settings) -> list[dict[str, Any]]:
    return [_metadata(name, path) for name, path in _managed_paths(settings.config_dir).items()]


def resolve_config_path(settings: Settings, name: str) -> Path:
    normalized = PurePosixPath(name)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("Fichier de configuration invalide")
    managed = _managed_paths(settings.config_dir)
    if name not in managed:
        raise ValueError("Fichier de configuration inconnu")
    return managed[name]


def read_config_file(settings: Settings, name: str) -> dict[str, Any]:
    path = resolve_config_path(settings, name)
    content = path.read_text(encoding="utf-8")
    return {**_metadata(name, path), "content": content, "checksum": _checksum(content)}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} doit être un objet YAML")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} doit être une liste YAML")
    return value


def _load_current(config_dir: Path, name: str) -> dict[str, Any]:
    path = config_dir / name
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, name)


def validate_config(settings: Settings, name: str, content: str) -> dict[str, Any]:
    if len(content.encode()) > MAX_CONFIG_BYTES:
        raise ValueError("Le fichier dépasse la limite de 500 Ko")
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        mark = getattr(exc, "problem_mark", None)
        location = f" ligne {mark.line + 1}, colonne {mark.column + 1}" if mark else ""
        raise ValueError(f"YAML invalide{location} : {problem}") from exc
    data = _mapping(parsed or {}, name)

    if name == "radio.yaml":
        for key in ("name", "tagline", "language", "timezone", "ai_disclosure", "health_notice"):
            if not str(data.get(key, "")).strip():
                raise ValueError(f"radio.yaml : `{key}` est obligatoire")
        stations = _list(data.get("stations"), "radio.yaml : `stations`")
        if not stations or len(set(map(str, stations))) != len(stations):
            raise ValueError("radio.yaml : les stations doivent être uniques et non vides")

    elif name == "characters.yaml":
        if "presenter" not in data:
            raise ValueError("characters.yaml : le rôle `presenter` est obligatoire")
        for role, profile in data.items():
            profile = _mapping(profile, f"characters.yaml : `{role}`")
            if not str(profile.get("name", "")).strip():
                raise ValueError(f"characters.yaml : `{role}.name` est obligatoire")
        schedule = _load_current(settings.config_dir, "schedule.yaml")
        used_roles = {
            str(role)
            for spec in schedule.get("formats", {}).values()
            for role in (spec.get("voices", []) if isinstance(spec, dict) else [])
        }
        missing = sorted(used_roles - set(map(str, data)))
        if missing:
            raise ValueError(f"characters.yaml : rôles utilisés par la grille absents : {', '.join(missing)}")

    elif name == "schedule.yaml":
        formats = _mapping(data.get("formats"), "schedule.yaml : `formats`")
        if not formats:
            raise ValueError("schedule.yaml : au moins un format est obligatoire")
        characters = _load_current(settings.config_dir, "characters.yaml")
        for identifier, raw_spec in formats.items():
            spec = _mapping(raw_spec, f"schedule.yaml : format `{identifier}`")
            minimum = float(spec.get("min_minutes", 0))
            maximum = float(spec.get("max_minutes", 0))
            if minimum <= 0 or maximum < minimum:
                raise ValueError(f"schedule.yaml : durée invalide pour `{identifier}`")
            voices = _list(spec.get("voices"), f"schedule.yaml : `{identifier}.voices`")
            unknown = [str(role) for role in voices if str(role) not in characters]
            if not voices or unknown:
                suffix = f" : {', '.join(unknown)}" if unknown else ""
                raise ValueError(f"schedule.yaml : casting invalide pour `{identifier}`{suffix}")
        _list(data.get("fixed_slots", []), "schedule.yaml : `fixed_slots`")

    elif name == "antenna.yaml":
        clock = _list(data.get("clock"), "antenna.yaml : `clock`")
        if not clock or not all(isinstance(item, str) and item.strip() for item in clock):
            raise ValueError("antenna.yaml : l’horloge doit contenir des catégories valides")
        for group in ("sound_effects", "spoken", "music"):
            entries = _list(data.get(group, []), f"antenna.yaml : `{group}`")
            identifiers = [str(item.get("id", "")) for item in entries if isinstance(item, dict)]
            if len(identifiers) != len(entries) or not all(identifiers):
                raise ValueError(f"antenna.yaml : chaque élément de `{group}` doit avoir un id")
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"antenna.yaml : ids dupliqués dans `{group}`")

    elif name == "video_series.yaml":
        _validate_identified_list(data, "series", name, required=("id", "name", "channel_url"))

    elif name == "lexicon.yaml":
        if not all(str(key).strip() and isinstance(value, str) and value.strip() for key, value in data.items()):
            raise ValueError("lexicon.yaml : chaque terme doit avoir une prononciation texte")

    elif name == "sources.yaml" or name.startswith("sources.d/"):
        _validate_identified_list(data, "sources", name, required=("id", "name", "kind", "url"))

    return data


def _validate_identified_list(
    data: dict[str, Any], key: str, filename: str, required: tuple[str, ...]
) -> None:
    entries = _list(data.get(key), f"{filename} : `{key}`")
    identifiers: list[str] = []
    for index, entry in enumerate(entries, start=1):
        entry = _mapping(entry, f"{filename} : `{key}` #{index}")
        for field in required:
            if not str(entry.get(field, "")).strip():
                raise ValueError(f"{filename} : `{key}` #{index}, `{field}` est obligatoire")
        identifiers.append(str(entry["id"]))
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{filename} : identifiants dupliqués dans `{key}`")


def save_config_file(
    settings: Settings, name: str, content: str, expected_checksum: str
) -> dict[str, Any]:
    path = resolve_config_path(settings, name)
    validate_config(settings, name, content)
    if not content.endswith("\n"):
        content += "\n"
    with CONFIG_WRITE_LOCK:
        current = path.read_text(encoding="utf-8")
        if _checksum(current) != expected_checksum:
            raise RuntimeError("Le fichier a changé depuis son ouverture. Recharge-le avant d’enregistrer.")
        now = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup = settings.data_dir / "config-backups" / now / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        original_stat = path.stat()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
            if hasattr(os, "chown"):
                os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {**read_config_file(settings, name), "backup": str(backup.relative_to(settings.data_dir))}
