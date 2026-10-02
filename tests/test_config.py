from pathlib import Path

from tests.helpers import settings


def test_sources_merge_main_file_and_fragments(tmp_path: Path):
    cfg = settings(tmp_path)
    config = tmp_path / "config"
    fragments = config / "sources.d"
    fragments.mkdir(parents=True)
    (config / "sources.yaml").write_text(
        "sources:\n  - id: primary\n    name: Primary\n    enabled: true\n",
        encoding="utf-8",
    )
    (fragments / "podcast.yaml").write_text(
        "sources:\n  - id: podcast\n    name: Podcast\n    history: all\n",
        encoding="utf-8",
    )
    object.__setattr__(cfg, "config_dir", config)

    assert cfg.sources == [
        {"id": "primary", "name": "Primary", "enabled": True},
        {"id": "podcast", "name": "Podcast", "history": "all"},
    ]
