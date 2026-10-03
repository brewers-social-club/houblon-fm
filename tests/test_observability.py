import json
from pathlib import Path

import pytest

from bsc_radio.db import Database
from bsc_radio.observability import (
    PipelineLog,
    pipeline_lock,
    reconcile_orphaned_runs,
    redact,
    sanitize_url,
    tail_log,
)
from tests.helpers import settings


def test_redaction_masks_structured_and_inline_secrets():
    protected = redact(
        {
            "api_key": "should-never-appear",
            "elevenlabs_api_key": "provider-secret",
            "message": (
                "Authorization: Bearer bearer-secret token=query-secret "
                "sk-live-1234567890123456 eyJheader.payload.signature"
            ),
        }
    )

    serialized = json.dumps(protected)
    assert "should-never-appear" not in serialized
    assert "provider-secret" not in serialized
    assert "bearer-secret" not in serialized
    assert "query-secret" not in serialized
    assert "sk-live" not in serialized
    assert "eyJheader" not in serialized
    assert serialized.count("[redacted]") >= 5
    assert sanitize_url("https://user:pass@example.test/feed?token=secret#fragment") == (
        "https://example.test/feed"
    )


def test_pipeline_log_writes_json_and_updates_database(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    log = PipelineLog(cfg, db, "collect", console=False)

    with log.stage("collect.source", source="test-source"):
        log.info("collect.summary", "Collecte terminée", items=3)
    log.finish("succeeded", items=3)

    documents = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [document["event"] for document in documents] == [
        "run.start",
        "collect.source.start",
        "collect.summary",
        "collect.source.done",
        "run.finish",
    ]
    assert {document["run_id"] for document in documents} == {log.run_id}
    assert all(document["timestamp"].endswith("+00:00") for document in documents)
    row = db.one(
        "SELECT status,finished_at,detail_json FROM pipeline_run WHERE id=?", (log.run_id,)
    )
    assert row["status"] == "succeeded"
    assert row["finished_at"]
    assert json.loads(row["detail_json"])["items"] == 3
    assert len(tail_log(log.path, 2)) == 2


def test_pipeline_failure_records_traceback_without_secret(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    log = PipelineLog(cfg, db, "run", console=False)

    try:
        raise RuntimeError("token=hidden-value")
    except RuntimeError as exc:
        log.fail(exc, stage="write")

    content = log.path.read_text(encoding="utf-8")
    assert "hidden-value" not in content
    assert "[redacted]" in content
    assert "traceback" in content
    row = db.one("SELECT status,detail_json FROM pipeline_run WHERE id=?", (log.run_id,))
    assert row["status"] == "failed"
    assert "hidden-value" not in row["detail_json"]


def test_orphaned_runs_are_reconciled_but_live_pid_is_preserved(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    dead_id = db.execute(
        """INSERT INTO pipeline_run(command,status,pid,heartbeat_at)
        VALUES('run','running',99999999,datetime('now','-10 minutes'))"""
    )
    live_id = db.execute(
        "INSERT INTO pipeline_run(command,status,pid) VALUES('collect','running',12345)"
    )

    assert reconcile_orphaned_runs(db) == 1
    dead = db.one("SELECT status,finished_at,detail_json FROM pipeline_run WHERE id=?", (dead_id,))
    live = db.one("SELECT status,finished_at FROM pipeline_run WHERE id=?", (live_id,))
    assert dead["status"] == "interrupted"
    assert dead["finished_at"]
    assert json.loads(dead["detail_json"])["reason"] == "process_missing"
    assert live["status"] == "running"
    assert live["finished_at"] is None


def test_orphan_recovery_closes_unfinished_source_fetch_without_live_pipeline(tmp_path: Path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    db.migrate()
    db.execute(
        """INSERT INTO source(id,name,kind,url,station)
        VALUES('source','Source','rss','https://example.test/feed','brassage')"""
    )
    fetch_id = db.execute(
        "INSERT INTO source_fetch(source_id,started_at) VALUES('source',CURRENT_TIMESTAMP)"
    )
    db.execute(
        """INSERT INTO pipeline_run(command,status,pid,heartbeat_at)
        VALUES('run','running',99999999,datetime('now','-10 minutes'))"""
    )

    assert reconcile_orphaned_runs(db) == 1
    fetch = db.one("SELECT finished_at,error FROM source_fetch WHERE id=?", (fetch_id,))
    assert fetch["finished_at"]
    assert "processus absent" in fetch["error"]


def test_pipeline_lock_rejects_a_concurrent_pipeline(tmp_path: Path):
    cfg = settings(tmp_path)
    with (
        pipeline_lock(cfg),
        pytest.raises(RuntimeError, match="déjà en cours"),
        pipeline_lock(cfg),
    ):
        pass
