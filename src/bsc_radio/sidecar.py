from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .admin_api import build_admin_router
from .antenna import AntennaClock
from .audio import ensure_fallback_audio
from .auth import AuthService
from .config import Settings
from .db import Database
from .scheduler import Scheduler


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    db = Database(settings.database)
    db.migrate()
    ensure_fallback_audio(settings.data_dir / "fallback.mp3")
    auth = AuthService(db)
    scheduler = Scheduler(settings, db)
    antenna = AntennaClock(settings, db, scheduler)
    app = FastAPI(title="Houblon FM sidecar", version="0.1.0")

    def admin_token(login: str, expires_at: int) -> str:
        payload = f"{login}:{expires_at}".encode()
        signature = hmac.new(settings.session_secret.encode(), payload, hashlib.sha256).hexdigest()
        return urlsafe_b64encode(payload + b":" + signature.encode()).decode().rstrip("=")

    def valid_admin_token(token: str | None) -> bool:
        if not token:
            return False
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = urlsafe_b64decode(padded.encode())
            payload, signature = decoded.rsplit(b":", 1)
            login, expires = payload.decode().rsplit(":", 1)
            expected = hmac.new(settings.session_secret.encode(), payload, hashlib.sha256).hexdigest()
            return (
                hmac.compare_digest(signature.decode(), expected)
                and hmac.compare_digest(login, settings.admin_login)
                and int(expires) >= int(time.time())
            )
        except (ValueError, TypeError, UnicodeDecodeError):
            return False

    def current_user(radio_session: Annotated[str | None, Cookie()] = None):
        return auth.user_for_session(radio_session)

    def required_user(user=Depends(current_user)):
        if not user:
            raise HTTPException(401, "Compte requis")
        return user

    def required_admin(request: Request):
        if valid_admin_token(request.cookies.get("radio_admin_session")):
            return
        supplied = request.headers.get("x-radio-admin-password", "")
        if not hmac.compare_digest(supplied.encode(), settings.admin_password.encode()):
            raise HTTPException(401, "Administration protégée")

    @app.get("/health")
    def health():
        return {"ok": True, "version": "0.1.0", "database": db.one("SELECT 1")[0] == 1}

    @app.get("/next", response_class=PlainTextResponse)
    def next_track():
        track = antenna.next()
        if not track:
            fallback = settings.data_dir / "fallback.mp3"
            if not fallback.exists():
                raise HTTPException(503, "Aucun audio disponible")
            return str(fallback)
        stored_path = Path(track.path)
        playable_path = (
            stored_path
            if stored_path.is_file()
            else settings.data_dir / "audio" / stored_path.name
        )
        if not playable_path.is_file():
            fallback = settings.data_dir / "fallback.mp3"
            if not fallback.exists():
                raise HTTPException(503, "Audio programmé introuvable")
            return str(fallback)
        antenna.mark_started(track)
        return str(playable_path)

    @app.get("/api/now")
    def now_playing():
        current = antenna.now()
        if current:
            return current
        row = db.one(
            """SELECT e.title,e.format,e.slug,a.started_at,au.duration_seconds FROM airing a
            JOIN episode e ON e.id=a.episode_id JOIN audio au ON au.id=a.audio_id ORDER BY a.started_at DESC LIMIT 1"""
        )
        return dict(row) if row else None

    @app.get("/api/upcoming")
    def upcoming():
        result = []
        excluded: set[int] = set()
        for _ in range(3):
            track = scheduler.next()
            if not track or track.episode_id in excluded:
                break
            result.append({"title": track.title, "format": track.format})
            excluded.add(track.episode_id)
        return result

    @app.post("/api/register")
    def register(email: Annotated[str, Form()], password: Annotated[str, Form()]):
        try:
            user_id = auth.register(email, password)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "user_id": user_id, "confirmation_required": True}

    @app.post("/api/login")
    def login(response: Response, email: Annotated[str, Form()], password: Annotated[str, Form()]):
        token = auth.login(email, password)
        if not token:
            raise HTTPException(401, "Identifiants invalides")
        response.set_cookie(
            "radio_session",
            token,
            httponly=True,
            secure=settings.site_url.startswith("https"),
            samesite="lax",
            max_age=30 * 86400,
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response, radio_session: Annotated[str | None, Cookie()] = None):
        if radio_session:
            auth.logout(radio_session)
        response.delete_cookie("radio_session")
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(required_user)):
        return {"id": user["id"], "email": user["email"], "plan": user["plan"]}

    @app.post("/api/feedback")
    def feedback(
        message: Annotated[str, Form()],
        rating: Annotated[int | None, Form()] = None,
        redirect_to: Annotated[str | None, Form()] = None,
        user=Depends(current_user),
    ):
        if len(message.strip()) < 3:
            raise HTTPException(400, "Message trop court")
        normalized_message = message.strip()
        duplicate = db.one(
            """SELECT id FROM feedback
            WHERE lower(trim(message))=lower(?)
            AND created_at >= datetime('now','-10 minutes')
            ORDER BY id DESC LIMIT 1""",
            (normalized_message,),
        )
        identifier = (
            int(duplicate["id"])
            if duplicate
            else db.execute(
                "INSERT INTO feedback(user_id,rating,message) VALUES(?,?,?)",
                (user["id"] if user else None, rating, normalized_message),
            )
        )
        if redirect_to == "/merci-avis.html":
            return RedirectResponse(redirect_to, status_code=303)
        return {"ok": True, "id": identifier, "duplicate": bool(duplicate)}

    @app.get("/api/replays")
    def replays(user=Depends(required_user)):
        return [
            dict(row)
            for row in db.all(
                "SELECT id,slug,title,format,summary,published_at FROM episode WHERE status='published' AND hidden=0 ORDER BY published_at DESC"
            )
        ]

    @app.get("/audio/{filename}")
    def protected_audio(
        filename: str, podcast_token: str | None = None, user=Depends(current_user)
    ):
        if not user and not podcast_token:
            raise HTTPException(401, "Compte ou jeton podcast requis")
        path = (settings.data_dir / "audio" / Path(filename).name).resolve()
        if path.parent != (settings.data_dir / "audio").resolve() or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/mpeg")

    @app.get("/api/stats/public")
    def public_stats():
        episodes = db.one(
            "SELECT COUNT(*) count FROM episode WHERE status='published' AND hidden=0"
        )["count"]
        hours = db.one("SELECT COALESCE(SUM(duration_seconds),0)/3600 hours FROM audio")["hours"]
        costs = db.one("SELECT COALESCE(SUM(cost_micro_eur),0)/1000000.0 eur FROM cost")["eur"]
        return {"episodes": episodes, "hours": round(hours, 1), "monthly_cost_eur": round(costs, 2)}

    @app.post("/api/admin/login")
    def admin_login(
        response: Response,
        login: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ):
        login_ok = hmac.compare_digest(login.strip().encode(), settings.admin_login.encode())
        password_ok = hmac.compare_digest(password.encode(), settings.admin_password.encode())
        if not (login_ok and password_ok):
            raise HTTPException(401, "Identifiants invalides")
        max_age = 12 * 60 * 60
        response.set_cookie(
            "radio_admin_session",
            admin_token(settings.admin_login, int(time.time()) + max_age),
            httponly=True,
            secure=settings.site_url.startswith("https"),
            samesite="lax",
            max_age=max_age,
            path="/",
        )
        return {"ok": True, "login": settings.admin_login}

    @app.post("/api/admin/logout")
    def admin_logout(response: Response):
        response.delete_cookie("radio_admin_session", path="/")
        return {"ok": True}

    @app.get("/api/admin/session", dependencies=[Depends(required_admin)])
    def admin_session():
        return {"authenticated": True, "login": settings.admin_login}

    @app.get("/api/admin/dashboard", dependencies=[Depends(required_admin)])
    def admin_dashboard():
        return {
            "incidents": [
                dict(row)
                for row in db.all(
                    "SELECT * FROM incident WHERE status='open' ORDER BY created_at DESC"
                )
            ],
            "tasks": [
                dict(row)
                for row in db.all("SELECT * FROM task WHERE status IN ('pending','running')")
            ],
            "feedback": [
                dict(row) for row in db.all("SELECT * FROM feedback WHERE status='pending'")
            ],
            "cost_eur": db.one("SELECT COALESCE(SUM(cost_micro_eur),0)/1000000.0 value FROM cost")[
                "value"
            ],
        }

    @app.post("/api/admin/episode/{episode_id}/hide", dependencies=[Depends(required_admin)])
    def hide_episode(episode_id: int, hidden: bool = True):
        db.execute("UPDATE episode SET hidden=? WHERE id=?", (int(hidden), episode_id))
        return {"ok": True}

    @app.post("/api/admin/lexicon", dependencies=[Depends(required_admin)])
    def update_lexicon(term: Annotated[str, Form()], pronunciation: Annotated[str, Form()]):
        db.execute(
            """INSERT INTO lexicon_entry(term,pronunciation) VALUES(?,?)
            ON CONFLICT(term) DO UPDATE SET pronunciation=excluded.pronunciation,updated_at=CURRENT_TIMESTAMP""",
            (term.strip(), pronunciation.strip()),
        )
        return {"ok": True}

    app.include_router(build_admin_router(settings, db, required_admin))

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request):
        secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "Stripe non configuré")
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        parts = dict(part.split("=", 1) for part in signature.split(",") if "=" in part)
        signed = f"{parts.get('t', '')}.".encode() + payload
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(parts.get("v1", ""), expected):
            raise HTTPException(400, "Signature invalide")
        event = json.loads(payload)
        if event.get("type") in {"checkout.session.completed", "customer.subscription.updated"}:
            email = event.get("data", {}).get("object", {}).get("customer_details", {}).get("email")
            if email:
                db.execute("UPDATE user SET plan='support' WHERE email=? COLLATE NOCASE", (email,))
        return JSONResponse({"received": True})

    return app


app = create_app()
