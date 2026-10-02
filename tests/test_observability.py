import json
from pathlib import Path

from bsc_radio.db import Database
from bsc_radio.observability import PipelineLog, redact, sanitize_url, tail_log
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
