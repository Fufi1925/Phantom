from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings, get_settings

DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTH = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/oauth2/token"

# identify = user profile
# guilds = list servers
# guilds.join = allow bot to join guilds on behalf of user (if used later)
OAUTH_SCOPES = "identify guilds guilds.join"


def _serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    settings = settings or get_settings()
    return URLSafeTimedSerializer(
        settings.phantom_secret_key,
        salt="phantom-session-v1",
    )


def create_session_token(user: dict[str, Any], settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    payload = {
        "uid": int(user["id"]),
        "username": user.get("username") or "",
        "global_name": user.get("global_name"),
        "avatar": user.get("avatar"),
        "iat": int(time.time()),
    }
    return _serializer(settings).dumps(payload)


def read_session_token(token: str, settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    try:
        data = _serializer(settings).loads(
            token, max_age=settings.phantom_session_max_age
        )
        if not isinstance(data, dict) or "uid" not in data:
            return None
        return data
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def get_session_user(request: Request) -> dict[str, Any] | None:
    settings = get_settings()
    raw = request.cookies.get(settings.phantom_cookie_name)
    if not raw:
        return None
    return read_session_token(raw, settings)


def require_session_user(request: Request) -> dict[str, Any]:
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return user


def make_oauth_state() -> str:
    return secrets.token_urlsafe(24)


def oauth_authorize_url(state: str, settings: Settings | None = None) -> str:
    """Simple Discord login — identify + guilds + guilds.join."""
    settings = settings or get_settings()
    params = {
        "client_id": settings.phantom_discord_client_id,
        "response_type": "code",
        "redirect_uri": settings.oauth_redirect_uri,
        "scope": OAUTH_SCOPES,
        "state": state,
    }
    return f"{DISCORD_AUTH}?{urlencode(params)}"


def oauth_authorize_url_force(state: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    params = {
        "client_id": settings.phantom_discord_client_id,
        "response_type": "code",
        "redirect_uri": settings.oauth_redirect_uri,
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "consent",
    }
    return f"{DISCORD_AUTH}?{urlencode(params)}"


async def exchange_code(code: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    data = {
        "client_id": settings.phantom_discord_client_id,
        "client_secret": settings.phantom_discord_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oauth_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            DISCORD_TOKEN,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=400,
                detail=f"oauth_token_failed:{resp.text[:200]}",
            )
        return resp.json()


async def fetch_discord_user(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=400, detail="oauth_user_failed")
        return resp.json()


async def fetch_user_guilds(access_token: str) -> list[dict[str, Any]]:
    """Fetch guilds with member counts when available."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{DISCORD_API}/users/@me/guilds",
            params={"with_counts": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []


def avatar_url(user_id: int | str, avatar: str | None, discriminator: str | None = None) -> str:
    if avatar:
        ext = "gif" if str(avatar).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{ext}?size=128"
    try:
        idx = (int(user_id) >> 22) % 6
    except Exception:
        idx = 0
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def can_manage_guild(guild: dict[str, Any]) -> bool:
    """Owner or ADMINISTRATOR or MANAGE_GUILD."""
    if guild.get("owner"):
        return True
    try:
        perms = int(guild.get("permissions") or 0)
    except (TypeError, ValueError):
        return False
    ADMIN = 0x8
    MANAGE_GUILD = 0x20
    return bool(perms & ADMIN or perms & MANAGE_GUILD)


def bot_invite_url(client_id: str) -> str:
    # Manage channels, view channels, send messages, embed, attach, read history, manage messages
    perms = 0x10 | 0x400 | 0x800 | 0x4000 | 0x8000 | 0x10000 | 0x2000
    params = {
        "client_id": client_id,
        "permissions": str(perms),
        "scope": "bot applications.commands",
    }
    return f"{DISCORD_AUTH}?{urlencode(params)}"
