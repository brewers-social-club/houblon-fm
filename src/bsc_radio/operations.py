from __future__ import annotations

import datetime as dt
import json
import smtplib
import socket
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from .config import Settings
from .db import Database


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CostLedger:
    settings: Settings
    db: Database

    def month_total_eur(self, now: dt.datetime | None = None) -> float:
        now = now or dt.datetime.now(dt.UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return float(
            self.db.one(
                "SELECT COALESCE(SUM(cost_micro_eur),0)/1000000.0 total FROM cost WHERE created_at>=?",
                (start,),
            )["total"]
        )

    def assert_available(self, estimated_eur: float = 0) -> None:
        if self.month_total_eur() + estimated_eur > self.settings.budget_monthly_eur:
            raise BudgetExceeded(
                f"Budget mensuel de {self.settings.budget_monthly_eur:.2f} € atteint"
            )

    def record(
        self,
        provider: str,
        operation: str,
        model: str,
        cost_eur: float,
        units_in: int = 0,
        units_out: int = 0,
        request_id: str | None = None,
    ) -> int:
        return self.db.execute(
            """INSERT INTO cost(provider,operation,model,units_in,units_out,cost_micro_eur,request_id)
            VALUES(?,?,?,?,?,?,?)""",
            (
                provider,
                operation,
                model,
                units_in,
                units_out,
                round(cost_eur * 1_000_000),
                request_id,
            ),
        )


@dataclass
class IncidentService:
    settings: Settings
    db: Database

    def report(self, kind: str, message: str, context: dict[str, Any] | None = None) -> int:
        identifier = self.db.execute(
            "INSERT INTO incident(kind,message,context_json) VALUES(?,?,?)",
            (kind, message, json.dumps(context or {}, ensure_ascii=False)),
        )
        self.alert_if_due(kind, message)
        return identifier

    def alert_if_due(self, kind: str, message: str) -> bool:
        threshold = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=6)).isoformat()
        recent = self.db.one(
            "SELECT 1 FROM incident WHERE kind=? AND last_alert_at>=? LIMIT 1", (kind, threshold)
        )
        if recent:
            return False
        host = __import__("os").getenv("SMTP_HOST", "")
        target = __import__("os").getenv("ALERT_EMAIL_TO", "")
        if not host or not target:
            return False
        email = EmailMessage()
        email["Subject"] = f"[Houblon FM] Incident {kind}"
        email["From"] = __import__("os").getenv("ALERT_EMAIL_FROM", "radio@localhost")
        email["To"] = target
        email.set_content(message)
        with smtplib.SMTP(
            host, int(__import__("os").getenv("SMTP_PORT", "587")), timeout=15
        ) as smtp:
            smtp.starttls()
            user = __import__("os").getenv("SMTP_USER", "")
            if user:
                smtp.login(user, __import__("os").getenv("SMTP_PASSWORD", ""))
            smtp.send_message(email)
        self.db.execute(
            "UPDATE incident SET last_alert_at=CURRENT_TIMESTAMP WHERE kind=? AND status='open'",
            (kind,),
        )
        return True


@dataclass
class TaskQueue:
    db: Database

    def enqueue(self, kind: str, payload: dict[str, Any], available_at: str | None = None) -> int:
        return self.db.execute(
            "INSERT INTO task(kind,payload_json,available_at) VALUES(?,?,COALESCE(?,CURRENT_TIMESTAMP))",
            (kind, json.dumps(payload, ensure_ascii=False), available_at),
        )

    def lease(self, seconds: int = 300):
        now = dt.datetime.now(dt.UTC)
        until = (now + dt.timedelta(seconds=seconds)).isoformat()
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT * FROM task WHERE status='pending' AND available_at<=? AND
                (leased_until IS NULL OR leased_until<?) ORDER BY available_at,id LIMIT 1""",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE task SET status='running',attempts=attempts+1,leased_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (until, row["id"]),
            )
            return dict(row) | {"payload": json.loads(row["payload_json"]), "leased_until": until}

    def complete(self, task_id: int) -> None:
        self.db.execute(
            "UPDATE task SET status='done',leased_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id,),
        )

    def fail(self, task_id: int, error: str, retry_minutes: int = 15) -> None:
        available = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=retry_minutes)).isoformat()
        self.db.execute(
            "UPDATE task SET status='pending',error=?,available_at=?,leased_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error[:2000], available, task_id),
        )


def check_domains(db: Database, domains: list[str]) -> list[dict[str, str]]:
    changes = []
    now = dt.datetime.now(dt.UTC).isoformat()
    for domain in domains:
        try:
            socket.getaddrinfo(domain, 443)
            status = "registered"
        except socket.gaierror:
            status = "unresolved"
        previous = db.one("SELECT last_status FROM domain_watch WHERE domain=?", (domain,))
        if previous and previous["last_status"] != status:
            changes.append({"domain": domain, "before": previous["last_status"], "after": status})
        db.execute(
            """INSERT INTO domain_watch(domain,last_status,checked_at,changed_at) VALUES(?,?,?,?)
            ON CONFLICT(domain) DO UPDATE SET last_status=excluded.last_status,checked_at=excluded.checked_at,
            changed_at=CASE WHEN domain_watch.last_status!=excluded.last_status THEN excluded.changed_at ELSE domain_watch.changed_at END""",
            (domain, status, now, now),
        )
    return changes
