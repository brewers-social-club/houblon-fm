import datetime as dt
from pathlib import Path

import pytest

from bsc_radio.db import Database
from bsc_radio.operations import BudgetExceeded, CostLedger, TaskQueue
from bsc_radio.scheduler import Scheduler
from tests.helpers import settings


def audio_episode(db, ident, fmt, title):
    db.execute(
        "INSERT INTO episode(id,slug,title,format,station,status) VALUES(?,?,?,?,?,'audio')",
        (ident, f"e{ident}", title, fmt, "brasserie"),
    )
    db.execute(
        "INSERT INTO audio(id,episode_id,path,duration_seconds,checksum) VALUES(?,?,?,?,?)",
        (ident, ident, f"/tmp/{ident}.mp3", 60, "x"),
    )


def test_scheduler_avoids_same_episode_and_format(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    audio_episode(db, 1, "journal", "J")
    audio_episode(db, 2, "journal", "J2")
    audio_episode(db, 3, "style", "S")
    db.execute(
        "INSERT INTO airing(episode_id,audio_id,started_at) VALUES(1,1,?)",
        (dt.datetime.now(dt.UTC).isoformat(),),
    )
    track = Scheduler(cfg, db).next(dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC))
    assert track.episode_id == 3


def test_task_lease_is_exclusive(tmp_path: Path):
    db = Database(settings(tmp_path).database)
    db.migrate()
    queue = TaskQueue(db)
    identifier = queue.enqueue("voice", {"episode": 1})
    assert queue.lease()["id"] == identifier
    assert queue.lease() is None
    queue.complete(identifier)
    assert db.one("SELECT status FROM task WHERE id=?", (identifier,))["status"] == "done"


def test_budget_guard(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    ledger = CostLedger(cfg, db)
    ledger.record("x", "write", "m", 24.9)
    with pytest.raises(BudgetExceeded):
        ledger.assert_available(0.2)
