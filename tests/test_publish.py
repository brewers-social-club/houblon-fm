import json
from pathlib import Path

from bsc_radio.db import Database
from bsc_radio.publish import Publisher
from tests.helpers import settings


def test_empty_site_is_generated(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    result = Publisher(cfg, db).publish()
    assert result == {"episodes": 0}
    assert "Houblon FM" in (cfg.site_dir / "index.html").read_text()
    assert json.loads((cfg.site_dir / "data/search.json").read_text()) == []
