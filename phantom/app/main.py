from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth, db as dbmod
from app.config import get_settings
import time as _time

log = logging.getLogger("phantom")

APP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))

def _format_timestamp(ts: int | None) -> str:
    if not ts:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromtimestamp(int(ts))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts)

TEMPLATES.env.filters["timestamp"] = _format_timestamp

# Module-level DB: request.app is the PARENT app when mounted under University,
# so app.state.db on the sub-app is unreliable. Always use this helper.
_db_conn = None
_db_lock = None


def render(request: Request, template_name: str, context: dict[str, Any]) -> HTMLResponse:
    template = TEMPLATES.env.get_template(template_name)
    return HTMLResponse(template.render(context))


async def get_db():
    """Lazy shared SQLite connection for Phantom."""
    global _db_conn, _db_lock
    import asyncio

    if _db_lock is None:
        _db_lock = asyncio.Lock()
    async with _db_lock:
        if _db_conn is None:
            settings = get_settings()
            # Prefer /data on Railway if available
            try:
                if settings.db_path:
                    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            _db_conn = await dbmod.connect()
            log.info("Phantom DB ready at %s", get_settings().db_path)
        return _db_conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort init (also works when mounted)
    try:
        await get_db()
    except Exception as exc:
        log.error("Phantom DB init failed: %s", exc)
    yield
    global _db_conn
    if _db_conn is not None:
        try:
            await _db_conn.close()
        except Exception:
            pass
        _db_conn = None


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Phantom Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        root_path="",
    )

    app.mount(
        "/static",
        StaticFiles(directory=str(APP_DIR / "static")),
        name="static",
    )

    def _href(path: str) -> str:
        prefix = (settings.root_path or "").rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return f"{prefix}{path}" if prefix else path

    def _cookie_secure(request: Request) -> bool:
        # Railway / reverse proxy
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
        return proto == "https"

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        s = get_settings()
        user = auth.get_session_user(request)
        base = {
            "request": request,
            "brand": s.phantom_brand_name,
            "footer": s.phantom_footer,
            "base_url": s.base_url,
            "root_path": s.root_path or "",
            "user": user,
            "avatar_url": (
                auth.avatar_url(user["uid"], user.get("avatar")) if user else None
            ),
            "flash": request.cookies.get("phantom_flash"),
        }
        base.update(extra)
        return base

    def set_flash(response: Response, message: str, request: Request | None = None) -> None:
        s = get_settings()
        secure = _cookie_secure(request) if request is not None else True
        # Cookies must be latin-1 safe
        safe = (
            str(message)
            .replace("„", "'")
            .replace("“", "'")
            .replace("”", "'")
            .replace("–", "-")
            .replace("—", "-")
            .encode("latin-1", errors="replace")
            .decode("latin-1")
        )[:180]
        response.set_cookie(
            "phantom_flash",
            safe,
            max_age=30,
            path=s.phantom_cookie_path or "/",
            httponly=False,
            samesite="lax",
            secure=secure,
        )

    def clear_flash(response: Response) -> None:
        s = get_settings()
        response.delete_cookie("phantom_flash", path=s.phantom_cookie_path or "/")

    def set_session_cookie(response: Response, token: str, request: Request) -> None:
        s = get_settings()
        response.set_cookie(
            s.phantom_cookie_name,
            token,
            max_age=s.phantom_session_max_age,
            path=s.phantom_cookie_path or "/",
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
        )

    # ── Pages ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        user = auth.get_session_user(request)
        if user:
            return RedirectResponse(url=_href("/dashboard"), status_code=302)
        return RedirectResponse(url=_href("/login"), status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        s = get_settings()
        user = auth.get_session_user(request)
        if user:
            return RedirectResponse(url=_href("/dashboard"), status_code=302)
        missing = []
        if not s.phantom_discord_client_id:
            missing.append("PHANTOM_DISCORD_CLIENT_ID")
        if not s.phantom_discord_client_secret:
            missing.append("PHANTOM_DISCORD_CLIENT_SECRET")
        if not s.phantom_secret_key or s.phantom_secret_key == "dev-only-change-me":
            missing.append("PHANTOM_SECRET_KEY")
        resp = render(
            request,
            "login.html",
            ctx(request, missing=missing, redirect_uri=s.oauth_redirect_uri),
        )
        clear_flash(resp)
        return resp

    @app.get("/auth/discord")
    async def auth_discord(request: Request, force: int = 0):
        s = get_settings()
        if not s.phantom_discord_client_id or not s.phantom_discord_client_secret:
            resp = RedirectResponse(url=_href("/login"), status_code=302)
            set_flash(resp, "OAuth ist nicht konfiguriert (CLIENT_ID/SECRET).", request)
            return resp
        state = auth.make_oauth_state()
        url = (
            auth.oauth_authorize_url_force(state, s)
            if force
            else auth.oauth_authorize_url(state, s)
        )
        resp = RedirectResponse(url=url, status_code=302)
        # Path must match /phantom so browser sends cookie on callback
        resp.set_cookie(
            "phantom_oauth_state",
            state,
            max_age=600,
            path=s.phantom_cookie_path or "/",
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
        )
        return resp

    @app.get("/auth/callback")
    async def auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ):
        s = get_settings()
        if error:
            msg = error_description or error
            resp = RedirectResponse(url=_href("/login"), status_code=302)
            set_flash(resp, f"Discord Login abgebrochen: {msg}", request)
            return resp

        expected = request.cookies.get("phantom_oauth_state")
        if not code:
            resp = RedirectResponse(url=_href("/login"), status_code=302)
            set_flash(resp, "Kein OAuth-Code von Discord erhalten.", request)
            return resp
        if not state or not expected or state != expected:
            resp = RedirectResponse(url=_href("/login"), status_code=302)
            set_flash(
                resp,
                "OAuth-State ungültig/abgelaufen. Bitte erneut über 'Mit Discord anmelden' starten.",
                request,
            )
            return resp

        try:
            token_data = await auth.exchange_code(code, s)
            access = token_data.get("access_token")
            if not access:
                raise HTTPException(status_code=400, detail="no_access_token")
            duser = await auth.fetch_discord_user(access)
            if not duser.get("id"):
                raise HTTPException(status_code=400, detail="no_user_id")
            expires_in = int(token_data.get("expires_in") or 0)

            db = await get_db()
            scopes = token_data.get("scope") or auth.OAUTH_SCOPES
            await dbmod.upsert_user(
                db,
                user_id=int(duser["id"]),
                username=duser.get("username") or "user",
                global_name=duser.get("global_name"),
                avatar=duser.get("avatar"),
                access_token=access,
                refresh_token=token_data.get("refresh_token"),
                token_expires_at=int(time.time()) + expires_in if expires_in else None,
                scopes=scopes if isinstance(scopes, str) else " ".join(scopes),
            )
            # Store guild list (identify + guilds + guilds.join authorized)
            try:
                guilds_raw = await auth.fetch_user_guilds(access)
                await dbmod.replace_user_guilds(db, int(duser["id"]), guilds_raw)
            except Exception:
                log.exception("Failed to store user guilds after login")
            session = auth.create_session_token(duser, s)
        except HTTPException as e:
            log.exception("Phantom OAuth HTTPException")
            resp = RedirectResponse(url=_href("/login"), status_code=302)
            set_flash(resp, f"Login fehlgeschlagen: {e.detail}", request)
            return resp
        except Exception as e:
            log.exception("Phantom OAuth failed")
            resp = RedirectResponse(url=_href("/login"), status_code=302)
            # show a bit more than just AttributeError
            detail = f"{type(e).__name__}: {e}"
            set_flash(resp, f"Login Fehler: {detail[:140]}", request)
            return resp

        resp = RedirectResponse(url=_href("/dashboard"), status_code=302)
        set_session_cookie(resp, session, request)
        resp.delete_cookie("phantom_oauth_state", path=s.phantom_cookie_path or "/")
        set_flash(resp, "Erfolgreich angemeldet.", request)
        return resp

    @app.get("/auth/logout")
    async def logout(request: Request):
        s = get_settings()
        resp = RedirectResponse(url=_href("/login"), status_code=302)
        resp.delete_cookie(s.phantom_cookie_name, path=s.phantom_cookie_path or "/")
        set_flash(resp, "Abgemeldet.", request)
        return resp

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        user = auth.get_session_user(request)
        if not user:
            return RedirectResponse(url=_href("/login"), status_code=302)

        db = await get_db()
        uid = int(user["uid"])
        load_error = None

        # IMPORTANT: Only show guilds where the Phantom BOT is actually a member
        # (not all user guilds). This fulfills "no server config where bot is not present".
        bot_guilds = await dbmod.list_bot_guilds(db)

        # Also get user's manageable guilds (from their OAuth) so we can filter
        # only servers the user can manage + bot is in.
        user_manageable = set()
        row = await dbmod.get_user(db, uid)
        if row and row.get("access_token"):
            try:
                raw = await auth.fetch_user_guilds(row["access_token"])
                await dbmod.replace_user_guilds(db, uid, raw)
                for g in raw:
                    gdict = {
                        "id": str(g.get("id")),
                        "permissions": str(g.get("permissions") or "0"),
                        "owner": bool(g.get("owner")),
                    }
                    if auth.can_manage_guild(gdict):
                        user_manageable.add(int(g.get("id")))
            except Exception as e:
                log.exception("guild refresh failed")
                load_error = f"{type(e).__name__}: {e}"

        # Final list: only servers where BOT is present AND user can manage
        entries = []
        for g in bot_guilds:
            gid = int(g["guild_id"])
            if gid in user_manageable:
                entries.append({
                    "id": str(gid),
                    "name": g.get("name") or str(gid),
                    "icon": g.get("icon"),
                    "owner": False,  # we don't know owner here, but doesn't matter
                    "approximate_member_count": g.get("member_count"),
                    "bot_in": True,
                })

        entries.sort(
            key=lambda x: (
                -(x.get("approximate_member_count") or 0),
                (x.get("name") or "").lower(),
            )
        )

        # Main-bot style LIVE OVERVIEW stats (only Phantom scope)
        stats = await dbmod.get_phantom_stats(db)

        s = get_settings()
        invite_url = auth.bot_invite_url(s.phantom_discord_client_id) if s.phantom_discord_client_id else "#"

        resp = render(
            request,
            "dashboard.html",
            ctx(
                request,
                guilds=entries,
                page="home",
                load_error=load_error,
                invite_url=invite_url,
                first_name=(user.get("global_name") or user.get("username") or "there").split(" ")[0],
                bot_only=True,
                stats=stats,   # ← main-bot-like overview
            ),
        )
        clear_flash(resp)
        return resp

    @app.get("/dashboard/guild/{guild_id}", response_class=HTMLResponse)
    async def dashboard_guild(request: Request, guild_id: int):
        user = auth.get_session_user(request)
        if not user:
            return RedirectResponse(url=_href("/login"), status_code=302)

        db = await get_db()

        # CRITICAL: Only allow if the Phantom BOT is actually in this guild
        if not await dbmod.is_bot_in_guild(db, guild_id):
            resp = RedirectResponse(url=_href("/dashboard"), status_code=302)
            set_flash(resp, "Der Phantom-Bot ist auf diesem Server nicht vorhanden. Du kannst ihn nur auf Servern konfigurieren, auf denen er Mitglied ist.", request)
            return resp

        # Check user has manage rights (from their OAuth data)
        has_access = False
        row = await dbmod.get_user(db, int(user["uid"]))
        if row and row.get("access_token"):
            try:
                for g in await auth.fetch_user_guilds(row["access_token"]):
                    if int(g.get("id") or 0) == guild_id and auth.can_manage_guild(g):
                        has_access = True
                        guild = g
                        break
            except Exception:
                has_access = False

        if not has_access:
            resp = RedirectResponse(url=_href("/dashboard"), status_code=302)
            set_flash(resp, "Du hast keine Berechtigung, diesen Server zu bearbeiten.", request)
            return resp

        config = await dbmod.get_guild_config(db, guild_id)
        tickets = await dbmod.list_open_tickets(db, guild_id)

        # Live stats (main-bot style)
        live_stats = await dbmod.get_guild_live_stats(db, guild_id)

        resp = render(
            request,
            "guild.html",
            ctx(
                request,
                page="guild",
                guild=guild,
                config=config,
                tickets=tickets,
                live_stats=live_stats,
                staff_role_ids_json=json.dumps(config.get("staff_role_ids") or []),
            ),
        )
        clear_flash(resp)
        return resp

    @app.post("/dashboard/guild/{guild_id}/save")
    async def save_guild(
        request: Request,
        guild_id: int,
        panel_title: str = Form("Support Center"),
        panel_description: str = Form(""),
        panel_channel_id: str = Form(""),
        log_channel_id: str = Form(""),
        staff_role_ids: str = Form("[]"),
    ):
        user = auth.get_session_user(request)
        if not user:
            return RedirectResponse(url=_href("/login"), status_code=302)

        def _parse_id(raw: str) -> int | None:
            raw = (raw or "").strip()
            if raw.isdigit():
                return int(raw)
            return None

        try:
            roles = json.loads(staff_role_ids or "[]")
            if not isinstance(roles, list):
                roles = []
            roles = [int(x) for x in roles if str(x).isdigit()]
        except Exception:
            roles = []

        db = await get_db()
        await dbmod.save_guild_config(
            db,
            guild_id,
            panel_channel_id=_parse_id(panel_channel_id),
            log_channel_id=_parse_id(log_channel_id),
            staff_role_ids=roles,
            panel_title=panel_title.strip()[:120] or "Support Center",
            panel_description=panel_description.strip()[:1800]
            or "Klicke auf den Button, um ein Ticket zu öffnen.",
        )
        resp = RedirectResponse(url=_href(f"/dashboard/guild/{guild_id}"), status_code=302)
        set_flash(resp, "Einstellungen gespeichert.", request)
        return resp

    # ── JSON API ───────────────────────────────────────────

    @app.get("/api/health")
    async def api_health():
        s = get_settings()
        db_ok = False
        try:
            db = await get_db()
            await db.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        return {
            "ok": True,
            "service": "phantom",
            "brand": s.phantom_brand_name,
            "base_url": s.base_url,
            "db_ok": db_ok,
            "oauth_redirect": s.oauth_redirect_uri,
        }

    @app.get("/api/me")
    async def api_me(request: Request):
        user = auth.require_session_user(request)
        return {"user": user}

    @app.get("/api/me/guilds")
    async def api_me_guilds(request: Request, refresh: int = 0):
        """Guilds from DB (authorized via identify+guilds+guilds.join)."""
        user = auth.require_session_user(request)
        db = await get_db()
        uid = int(user["uid"])
        if refresh:
            row = await dbmod.get_user(db, uid)
            if row and row.get("access_token"):
                raw = await auth.fetch_user_guilds(row["access_token"])
                await dbmod.replace_user_guilds(db, uid, raw)
        guilds = await dbmod.list_user_guilds(db, uid)
        return {"guilds": guilds, "count": len(guilds)}

    @app.get("/api/guilds/{guild_id}/config")
    async def api_guild_config(request: Request, guild_id: int):
        s = get_settings()
        bot_key = request.headers.get("X-Phantom-Bot-Token")
        if not (bot_key and s.phantom_bot_token and bot_key == s.phantom_bot_token):
            auth.require_session_user(request)
        db = await get_db()
        cfg = await dbmod.get_guild_config(db, guild_id)
        return {"config": cfg}

    @app.get("/api/guilds/{guild_id}/tickets")
    async def api_guild_tickets(request: Request, guild_id: int):
        s = get_settings()
        bot_key = request.headers.get("X-Phantom-Bot-Token")
        if not (bot_key and s.phantom_bot_token and bot_key == s.phantom_bot_token):
            auth.require_session_user(request)
        db = await get_db()
        tickets = await dbmod.list_open_tickets(db, guild_id)
        return {"tickets": tickets}

    @app.post("/api/internal/tickets/register")
    async def api_register_ticket(request: Request):
        s = get_settings()
        bot_key = request.headers.get("X-Phantom-Bot-Token")
        if not (bot_key and s.phantom_bot_token and bot_key == s.phantom_bot_token):
            raise HTTPException(status_code=401, detail="bot_only")
        body = await request.json()
        db = await get_db()
        await dbmod.register_ticket(
            db,
            channel_id=int(body["channel_id"]),
            guild_id=int(body["guild_id"]),
            owner_id=int(body["owner_id"]),
            category=str(body.get("category") or "support"),
        )
        return {"ok": True}

    @app.post("/api/internal/tickets/{channel_id}/claim")
    async def api_claim_ticket(request: Request, channel_id: int):
        s = get_settings()
        bot_key = request.headers.get("X-Phantom-Bot-Token")
        if not (bot_key and s.phantom_bot_token and bot_key == s.phantom_bot_token):
            raise HTTPException(status_code=401, detail="bot_only")
        body = await request.json()
        claimed_by = body.get("claimed_by")
        db = await get_db()
        await dbmod.set_ticket_claim(
            db,
            channel_id,
            int(claimed_by) if claimed_by is not None else None,
        )
        return {"ok": True}

    @app.delete("/api/internal/tickets/{channel_id}")
    async def api_delete_ticket(request: Request, channel_id: int):
        s = get_settings()
        bot_key = request.headers.get("X-Phantom-Bot-Token")
        if not (bot_key and s.phantom_bot_token and bot_key == s.phantom_bot_token):
            raise HTTPException(status_code=401, detail="bot_only")
        db = await get_db()
        await dbmod.delete_ticket(db, channel_id)
        return {"ok": True}

    @app.get("/api/internal/guilds/{guild_id}/config")
    async def api_bot_guild_config(request: Request, guild_id: int):
        s = get_settings()
        bot_key = request.headers.get("X-Phantom-Bot-Token")
        if not (bot_key and s.phantom_bot_token and bot_key == s.phantom_bot_token):
            raise HTTPException(status_code=401, detail="bot_only")
        db = await get_db()
        cfg = await dbmod.get_guild_config(db, guild_id)
        return {"config": cfg}

    return app


app = create_app()
