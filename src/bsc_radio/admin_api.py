from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import FileResponse

from .config import Settings
from .db import Database
from .episodes import delete_episode
from .observability import reconcile_orphaned_runs, tail_log
from .publish import Publisher


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _page(limit: int, offset: int, rows: list[Any], total: int) -> dict[str, Any]:
    return {
        "items": [_dict(row) for row in rows],
        "pagination": {"limit": limit, "offset": offset, "total": total},
    }


def _audio_file(root: Path, candidate: Path) -> Path:
    managed_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(managed_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Audio introuvable") from exc
    if not resolved.is_file() or resolved.suffix.lower() != ".mp3":
        raise HTTPException(status_code=404, detail="Audio introuvable")
    return resolved


def build_admin_router(
    settings: Settings,
    db: Database,
    required_admin: Callable[..., None],
) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin",
        dependencies=[Depends(required_admin)],
    )

    @router.get("/overview")
    def overview():
        counts = _dict(
            db.one(
                """SELECT
                (SELECT COUNT(*) FROM episode) episodes,
                (SELECT COUNT(*) FROM episode WHERE status='published' AND hidden=0) published,
                (SELECT COUNT(*) FROM episode WHERE status IN ('draft','verified','audio','failed')) pending,
                (SELECT COUNT(*) FROM source WHERE enabled=1) active_sources,
                (SELECT COUNT(*) FROM item) collected_items,
                (SELECT COUNT(*) FROM item WHERE status='new') pending_items,
                (SELECT COUNT(*) FROM fact) facts,
                (SELECT COUNT(*) FROM topic) topics,
                (SELECT COUNT(*) FROM incident WHERE status='open') open_incidents,
                (SELECT COUNT(*) FROM task WHERE status IN ('pending','running')) active_tasks,
                (SELECT COALESCE(SUM(duration_seconds),0) FROM audio) audio_seconds,
                (SELECT COALESCE(SUM(cost_micro_eur),0)/1000000.0 FROM cost) cost_eur"""
            )
        )
        now = db.one("SELECT value,updated_at FROM kv WHERE key='antenna.now'")
        now_payload = _json(now["value"], {}) if now else {}
        if now:
            now_payload["started_at"] = now["updated_at"]
        last_run = db.one(
            "SELECT id,command,status,started_at,finished_at,detail_json FROM pipeline_run ORDER BY id DESC LIMIT 1"
        )
        last_fetch = db.one(
            """SELECT sf.*,s.name source_name FROM source_fetch sf JOIN source s ON s.id=sf.source_id
            ORDER BY sf.id DESC LIMIT 1"""
        )
        return {
            "counts": counts,
            "now": now_payload or None,
            "last_run": _dict(last_run),
            "last_fetch": _dict(last_fetch),
            "database_bytes": settings.database.stat().st_size if settings.database.exists() else 0,
        }

    @router.get("/episodes")
    def episodes(
        q: str = "",
        status: str = "",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        clauses: list[str] = []
        params: list[Any] = []
        if q.strip():
            clauses.append("(e.title LIKE ? OR e.summary LIKE ? OR e.slug LIKE ?)")
            needle = f"%{q.strip()}%"
            params.extend([needle, needle, needle])
        if status:
            clauses.append("e.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = db.one(f"SELECT COUNT(*) count FROM episode e {where}", tuple(params))["count"]
        rows = db.all(
            f"""SELECT e.id,e.slug,e.title,e.format,e.station,e.status,e.summary,e.hidden,
            e.created_at,e.updated_at,e.published_at,e.rejection_reason,
            a.duration_seconds,a.lufs,a.bitrate,
            (SELECT COUNT(*) FROM sentence s WHERE s.episode_id=e.id) sentence_count,
            (SELECT COUNT(*) FROM airing ar WHERE ar.episode_id=e.id) airing_count
            FROM episode e LEFT JOIN audio a ON a.episode_id=e.id {where}
            ORDER BY e.id DESC LIMIT ? OFFSET ?""",
            tuple([*params, limit, offset]),
        )
        return _page(limit, offset, rows, total)

    @router.get("/episodes/{episode_id}")
    def episode_detail(episode_id: int):
        episode = db.one(
            """SELECT e.*,a.id audio_id,a.path audio_path,a.duration_seconds,a.bitrate,a.lufs,
            a.checksum,a.created_at audio_created_at,ev.canonical_name event_name
            FROM episode e LEFT JOIN audio a ON a.episode_id=e.id
            LEFT JOIN event ev ON ev.id=e.event_id WHERE e.id=?""",
            (episode_id,),
        )
        if not episode:
            raise HTTPException(404, "Épisode introuvable")
        sentences = []
        for row in db.all(
            "SELECT * FROM sentence WHERE episode_id=? ORDER BY position", (episode_id,)
        ):
            item = _dict(row)
            fact_ids = _json(item.pop("fact_ids_json", "[]"), [])
            item["delivery"] = _json(item.pop("delivery_json", "[]"), [])
            item["fact_ids"] = fact_ids
            item["facts"] = [
                _dict(fact)
                for fact in (
                    db.all(
                        f"SELECT id,statement,fact_type,reliability,source_name,source_url,source_date FROM fact WHERE id IN ({','.join('?' for _ in fact_ids)})",
                        tuple(fact_ids),
                    )
                    if fact_ids
                    else []
                )
            ]
            sentences.append(item)
        return {
            "episode": _dict(episode),
            "script": _json(episode["script_json"], []),
            "sentences": sentences,
            "airings": [
                _dict(row)
                for row in db.all(
                    "SELECT id,started_at,ended_at,listener_peak,reason FROM airing WHERE episode_id=? ORDER BY id DESC LIMIT 100",
                    (episode_id,),
                )
            ],
            "feedback": [
                _dict(row)
                for row in db.all(
                    "SELECT id,rating,message,status,created_at FROM feedback WHERE episode_id=? ORDER BY id DESC",
                    (episode_id,),
                )
            ],
        }

    @router.get("/episodes/{episode_id}/audio")
    def episode_audio(episode_id: int):
        row = db.one(
            "SELECT path FROM audio WHERE episode_id=? ORDER BY id DESC LIMIT 1",
            (episode_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Audio introuvable")
        configured = Path(str(row["path"]))
        candidate = configured if configured.is_file() else settings.data_dir / "audio" / configured.name
        path = _audio_file(settings.data_dir / "audio", candidate)
        return FileResponse(path, media_type="audio/mpeg", filename=path.name)

    @router.post("/episodes/{episode_id}/visibility")
    def episode_visibility(episode_id: int, hidden: bool = Form()):
        if not db.one("SELECT 1 FROM episode WHERE id=?", (episode_id,)):
            raise HTTPException(404, "Épisode introuvable")
        db.execute("UPDATE episode SET hidden=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(hidden), episode_id))
        Publisher(settings, db).publish()
        return {"ok": True, "hidden": hidden}

    @router.post("/episodes/{episode_id}/delete")
    def episode_delete(episode_id: int, confirmation: str = Form()):
        if confirmation != f"DELETE {episode_id}":
            raise HTTPException(400, f"Saisissez DELETE {episode_id} pour confirmer")
        try:
            result = delete_episode(settings, db, episode_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, **result}

    @router.get("/sources")
    def sources():
        return [
            _dict(row)
            for row in db.all(
                """SELECT s.*,
                (SELECT COUNT(*) FROM item i WHERE i.source_id=s.id) item_count,
                (SELECT COUNT(*) FROM item i WHERE i.source_id=s.id AND i.status='new') pending_count,
                (SELECT started_at FROM source_fetch sf WHERE sf.source_id=s.id ORDER BY sf.id DESC LIMIT 1) last_started_at,
                (SELECT finished_at FROM source_fetch sf WHERE sf.source_id=s.id ORDER BY sf.id DESC LIMIT 1) last_finished_at,
                (SELECT item_count FROM source_fetch sf WHERE sf.source_id=s.id ORDER BY sf.id DESC LIMIT 1) last_item_count,
                (SELECT error FROM source_fetch sf WHERE sf.source_id=s.id ORDER BY sf.id DESC LIMIT 1) last_error
                FROM source s ORDER BY s.enabled DESC,s.name"""
            )
        ]

    @router.get("/sources/{source_id}/fetches")
    def source_fetches(source_id: str, limit: int = Query(50, ge=1, le=200)):
        return [
            _dict(row)
            for row in db.all(
                "SELECT * FROM source_fetch WHERE source_id=? ORDER BY id DESC LIMIT ?",
                (source_id, limit),
            )
        ]

    @router.get("/items")
    def items(
        q: str = "",
        source: str = "",
        status: str = "",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        clauses: list[str] = []
        params: list[Any] = []
        if q.strip():
            clauses.append("(i.title LIKE ? OR i.author LIKE ? OR i.raw_text LIKE ?)")
            needle = f"%{q.strip()}%"
            params.extend([needle, needle, needle])
        if source:
            clauses.append("i.source_id=?")
            params.append(source)
        if status:
            clauses.append("i.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = db.one(f"SELECT COUNT(*) count FROM item i {where}", tuple(params))["count"]
        rows = db.all(
            f"""SELECT i.id,i.source_id,s.name source_name,i.url,i.title,i.author,i.published_at,
            i.collected_at,i.status,LENGTH(COALESCE(i.raw_text,'')) content_length,
            (SELECT COUNT(*) FROM fact f WHERE f.item_id=i.id) fact_count
            FROM item i JOIN source s ON s.id=i.source_id {where}
            ORDER BY COALESCE(i.published_at,i.collected_at) DESC LIMIT ? OFFSET ?""",
            tuple([*params, limit, offset]),
        )
        return _page(limit, offset, rows, total)

    @router.get("/items/{item_id}")
    def item_detail(item_id: int):
        item = db.one(
            """SELECT i.*,s.name source_name,s.kind source_kind,s.station source_station
            FROM item i JOIN source s ON s.id=i.source_id WHERE i.id=?""",
            (item_id,),
        )
        if not item:
            raise HTTPException(404, "Contenu introuvable")
        payload = _dict(item)
        payload["metadata"] = _json(payload.pop("metadata_json", "{}"), {})
        return {
            "item": payload,
            "facts": [_dict(row) for row in db.all("SELECT * FROM fact WHERE item_id=? ORDER BY id", (item_id,))],
        }

    @router.get("/topics")
    def topics(
        q: str = "",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        where = "WHERE (t.title LIKE ? OR t.summary LIKE ?)" if q.strip() else ""
        params: tuple[Any, ...] = (f"%{q.strip()}%", f"%{q.strip()}%") if q.strip() else ()
        total = db.one(f"SELECT COUNT(*) count FROM topic t {where}", params)["count"]
        rows = db.all(
            f"""SELECT t.*,ev.canonical_name event_name,
            (SELECT COUNT(*) FROM topic_fact tf WHERE tf.topic_id=t.id) fact_count
            FROM topic t LEFT JOIN event ev ON ev.id=t.event_id {where}
            ORDER BY t.last_seen_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return _page(limit, offset, rows, total)

    @router.get("/topics/{topic_id}")
    def topic_detail(topic_id: int):
        topic = db.one("SELECT * FROM topic WHERE id=?", (topic_id,))
        if not topic:
            raise HTTPException(404, "Sujet introuvable")
        facts = db.all(
            """SELECT f.*,i.title item_title,i.url item_url FROM topic_fact tf
            JOIN fact f ON f.id=tf.fact_id JOIN item i ON i.id=f.item_id
            WHERE tf.topic_id=? ORDER BY f.reliability DESC,f.id""",
            (topic_id,),
        )
        return {"topic": _dict(topic), "facts": [_dict(row) for row in facts]}

    @router.get("/facts")
    def facts(
        q: str = "",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        where = "WHERE (f.statement LIKE ? OR f.source_name LIKE ?)" if q.strip() else ""
        params: tuple[Any, ...] = (f"%{q.strip()}%", f"%{q.strip()}%") if q.strip() else ()
        total = db.one(f"SELECT COUNT(*) count FROM fact f {where}", params)["count"]
        rows = db.all(
            f"""SELECT f.id,f.item_id,f.statement,f.fact_type,f.reliability,f.source_url,
            f.source_name,f.source_date,f.created_at,i.title item_title
            FROM fact f JOIN item i ON i.id=f.item_id {where}
            ORDER BY f.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return _page(limit, offset, rows, total)

    @router.get("/antenna")
    def antenna():
        configured: dict[str, dict[str, Any]] = {}
        for group in ("sound_effects", "spoken", "music"):
            for asset in settings.antenna.get(group, []):
                configured[str(asset.get("id"))] = {**asset, "group": group}
        assets: list[dict[str, Any]] = []
        root = settings.data_dir / "antenna"
        if root.exists():
            for path in sorted(root.glob("*/*.mp3")):
                stat = path.stat()
                assets.append(
                    {
                        **configured.get(path.stem, {}),
                        "id": path.stem,
                        "category": path.parent.name,
                        "filename": path.name,
                        "bytes": stat.st_size,
                        "updated_at": stat.st_mtime,
                    }
                )
        now = db.one("SELECT value,updated_at FROM kv WHERE key='antenna.now'")
        return {
            "enabled": settings.antenna.get("enabled", True),
            "clock": settings.antenna.get("clock", []),
            "now": ({**_json(now["value"], {}), "started_at": now["updated_at"]} if now else None),
            "assets": assets,
            "recent_airings": [
                _dict(row)
                for row in db.all(
                    """SELECT ar.id,ar.started_at,ar.ended_at,ar.listener_peak,ar.reason,
                    e.id episode_id,e.title,e.format,e.slug FROM airing ar
                    LEFT JOIN episode e ON e.id=ar.episode_id ORDER BY ar.id DESC LIMIT 100"""
                )
            ],
            "clock_index": _dict(db.one("SELECT value,updated_at FROM kv WHERE key='antenna.clock.index'")),
        }

    @router.get("/antenna/audio/{category}/{asset_id}")
    def antenna_audio(category: str, asset_id: str):
        token = re.compile(r"[a-z0-9][a-z0-9_-]{1,79}")
        if not token.fullmatch(category) or not token.fullmatch(asset_id):
            raise HTTPException(status_code=404, detail="Audio introuvable")
        path = _audio_file(
            settings.data_dir / "antenna",
            settings.data_dir / "antenna" / category / f"{asset_id}.mp3",
        )
        return FileResponse(path, media_type="audio/mpeg", filename=path.name)

    @router.get("/operations")
    def operations():
        reconcile_orphaned_runs(db)
        runs = []
        for row in db.all(
            "SELECT * FROM pipeline_run ORDER BY id DESC LIMIT 100"
        ):
            item = _dict(row)
            item["detail"] = _json(item.pop("detail_json", "{}"), {})
            runs.append(item)
        return {
            "runs": runs,
            "incidents": [_dict(row) for row in db.all("SELECT * FROM incident ORDER BY id DESC LIMIT 100")],
            "tasks": [_dict(row) for row in db.all("SELECT * FROM task ORDER BY id DESC LIMIT 100")],
            "feedback": [_dict(row) for row in db.all("SELECT * FROM feedback ORDER BY id DESC LIMIT 100")],
            "costs": [
                _dict(row)
                for row in db.all(
                    """SELECT provider,operation,model,COUNT(*) requests,
                    SUM(units_in) units_in,SUM(units_out) units_out,
                    SUM(cost_micro_eur)/1000000.0 cost_eur,MAX(created_at) last_at
                    FROM cost GROUP BY provider,operation,model ORDER BY cost_eur DESC"""
                )
            ],
        }

    @router.post("/incidents/{incident_id}/resolve")
    def resolve_incident(incident_id: int):
        db.execute(
            "UPDATE incident SET status='resolved',resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (incident_id,),
        )
        return {"ok": True}

    @router.get("/logs")
    def logs(lines: int = Query(200, ge=1, le=2000)):
        entries = []
        for line in tail_log(settings.data_dir / "logs" / "radio.jsonl", lines):
            entries.append(_json(line, {"message": line.rstrip()}))
        return entries

    return router
