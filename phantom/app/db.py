from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT NOT NULL,
    global_name TEXT,
    avatar      TEXT,
    access_token TEXT,
    refresh_token TEXT,
    token_expires_at INTEGER,
    scopes      TEXT,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_guilds (
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL,
    name        TEXT,
    icon        TEXT,
    owner       INTEGER DEFAULT 0,
    permissions TEXT,
    features    TEXT,
    approximate_member_count INTEGER,
    raw_json    TEXT,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_user_guilds_user ON user_guilds(user_id);
CREATE INDEX IF NOT EXISTS idx_user_guilds_guild ON user_guilds(guild_id);

CREATE TABLE IF NOT EXISTS guild_configs (
    guild_id            INTEGER PRIMARY KEY,
    panel_channel_id    INTEGER,
    log_channel_id      INTEGER,
    staff_role_ids      TEXT DEFAULT '[]',
    panel_message_id    INTEGER,
    panel_title         TEXT,
    panel_description   TEXT,
    updated_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS open_tickets (
    channel_id      INTEGER PRIMARY KEY,
    guild_id        INTEGER NOT NULL,
    owner_id        INTEGER NOT NULL,
    claimed_by      INTEGER,
    category        TEXT DEFAULT 'support',
    status          TEXT DEFAULT 'open',
    created_at      INTEGER NOT NULL,
    claimed_at      INTEGER,
    last_activity   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tickets_guild ON open_tickets(guild_id);
CREATE INDEX IF NOT EXISTS idx_tickets_owner ON open_tickets(owner_id);

-- Bot's own guilds (only servers where Phantom Bot is actually a member)
CREATE TABLE IF NOT EXISTS bot_guilds (
    guild_id    INTEGER PRIMARY KEY,
    name        TEXT,
    icon        TEXT,
    member_count INTEGER,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_guilds_updated ON bot_guilds(updated_at);
"""


async def connect() -> aiosqlite.Connection:
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    # lightweight migrations for older DBs
    try:
        await db.execute("ALTER TABLE users ADD COLUMN scopes TEXT")
        await db.commit()
    except Exception:
        pass
    await db.commit()
    return db


async def upsert_user(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    username: str,
    global_name: str | None,
    avatar: str | None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    token_expires_at: int | None = None,
    scopes: str | None = None,
) -> None:
    now = int(time.time())
    await db.execute(
        """
        INSERT INTO users (
            user_id, username, global_name, avatar,
            access_token, refresh_token, token_expires_at, scopes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            global_name=excluded.global_name,
            avatar=excluded.avatar,
            access_token=COALESCE(excluded.access_token, users.access_token),
            refresh_token=COALESCE(excluded.refresh_token, users.refresh_token),
            token_expires_at=COALESCE(excluded.token_expires_at, users.token_expires_at),
            scopes=COALESCE(excluded.scopes, users.scopes),
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            username,
            global_name,
            avatar,
            access_token,
            refresh_token,
            token_expires_at,
            scopes,
            now,
        ),
    )
    await db.commit()


async def get_user(db: aiosqlite.Connection, user_id: int) -> dict[str, Any] | None:
    cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def replace_user_guilds(
    db: aiosqlite.Connection,
    user_id: int,
    guilds: list[dict[str, Any]],
) -> int:
    """Replace stored guild list for a user. Returns count saved."""
    now = int(time.time())
    await db.execute("DELETE FROM user_guilds WHERE user_id = ?", (user_id,))
    for g in guilds:
        try:
            gid = int(g.get("id"))
        except (TypeError, ValueError):
            continue
        features = g.get("features") or []
        await db.execute(
            """
            INSERT INTO user_guilds (
                user_id, guild_id, name, icon, owner, permissions,
                features, approximate_member_count, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                gid,
                g.get("name"),
                g.get("icon"),
                1 if g.get("owner") else 0,
                str(g.get("permissions") or "0"),
                json.dumps(features, ensure_ascii=False),
                g.get("approximate_member_count"),
                json.dumps(g, ensure_ascii=False),
                now,
            ),
        )
    await db.commit()
    return len(guilds)


async def list_user_guilds(db: aiosqlite.Connection, user_id: int) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT * FROM user_guilds WHERE user_id = ? ORDER BY name COLLATE NOCASE",
        (user_id,),
    )
    rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["owner"] = bool(d.get("owner"))
        try:
            d["features"] = json.loads(d.get("features") or "[]")
        except json.JSONDecodeError:
            d["features"] = []
        try:
            d["raw"] = json.loads(d.get("raw_json") or "{}")
        except json.JSONDecodeError:
            d["raw"] = {}
        out.append(d)
    return out


async def get_guild_config(db: aiosqlite.Connection, guild_id: int) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM guild_configs WHERE guild_id = ?", (guild_id,))
    row = await cur.fetchone()
    if not row:
        return {
            "guild_id": guild_id,
            "panel_channel_id": None,
            "log_channel_id": None,
            "staff_role_ids": [],
            "panel_message_id": None,
            "panel_title": "Support Center",
            "panel_description": "Klicke auf den Button, um ein Ticket zu öffnen.",
            "updated_at": None,
        }
    data = dict(row)
    try:
        data["staff_role_ids"] = json.loads(data.get("staff_role_ids") or "[]")
    except json.JSONDecodeError:
        data["staff_role_ids"] = []
    return data


async def save_guild_config(
    db: aiosqlite.Connection,
    guild_id: int,
    *,
    panel_channel_id: int | None = None,
    log_channel_id: int | None = None,
    staff_role_ids: list[int] | None = None,
    panel_message_id: int | None = None,
    panel_title: str | None = None,
    panel_description: str | None = None,
) -> dict[str, Any]:
    current = await get_guild_config(db, guild_id)
    now = int(time.time())
    payload = {
        "guild_id": guild_id,
        "panel_channel_id": panel_channel_id
        if panel_channel_id is not None
        else current.get("panel_channel_id"),
        "log_channel_id": log_channel_id
        if log_channel_id is not None
        else current.get("log_channel_id"),
        "staff_role_ids": json.dumps(
            staff_role_ids if staff_role_ids is not None else current.get("staff_role_ids") or []
        ),
        "panel_message_id": panel_message_id
        if panel_message_id is not None
        else current.get("panel_message_id"),
        "panel_title": panel_title if panel_title is not None else current.get("panel_title"),
        "panel_description": panel_description
        if panel_description is not None
        else current.get("panel_description"),
        "updated_at": now,
    }
    await db.execute(
        """
        INSERT INTO guild_configs (
            guild_id, panel_channel_id, log_channel_id, staff_role_ids,
            panel_message_id, panel_title, panel_description, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            panel_channel_id=excluded.panel_channel_id,
            log_channel_id=excluded.log_channel_id,
            staff_role_ids=excluded.staff_role_ids,
            panel_message_id=excluded.panel_message_id,
            panel_title=excluded.panel_title,
            panel_description=excluded.panel_description,
            updated_at=excluded.updated_at
        """,
        (
            payload["guild_id"],
            payload["panel_channel_id"],
            payload["log_channel_id"],
            payload["staff_role_ids"],
            payload["panel_message_id"],
            payload["panel_title"],
            payload["panel_description"],
            payload["updated_at"],
        ),
    )
    await db.commit()
    return await get_guild_config(db, guild_id)


async def list_open_tickets(db: aiosqlite.Connection, guild_id: int) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT * FROM open_tickets WHERE guild_id = ? ORDER BY created_at DESC",
        (guild_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def register_ticket(
    db: aiosqlite.Connection,
    *,
    channel_id: int,
    guild_id: int,
    owner_id: int,
    category: str = "support",
) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO open_tickets
        (channel_id, guild_id, owner_id, claimed_by, category, status, created_at, claimed_at)
        VALUES (?, ?, ?, NULL, ?, 'open', ?, NULL)
        """,
        (channel_id, guild_id, owner_id, category, int(time.time())),
    )
    await db.commit()


async def set_ticket_claim(
    db: aiosqlite.Connection, channel_id: int, claimed_by: int | None
) -> None:
    if claimed_by is None:
        await db.execute(
            "UPDATE open_tickets SET claimed_by=NULL, claimed_at=NULL, status='open' WHERE channel_id=?",
            (channel_id,),
        )
    else:
        await db.execute(
            "UPDATE open_tickets SET claimed_by=?, claimed_at=?, status='claimed' WHERE channel_id=?",
            (claimed_by, int(time.time()), channel_id),
        )
    await db.commit()


async def delete_ticket(db: aiosqlite.Connection, channel_id: int) -> None:
    await db.execute("DELETE FROM open_tickets WHERE channel_id=?", (channel_id,))
    await db.commit()


async def update_ticket_activity(db: aiosqlite.Connection, channel_id: int) -> None:
    """Update last_activity timestamp (for live feeling)."""
    now = int(time.time())
    await db.execute(
        "UPDATE open_tickets SET last_activity = ? WHERE channel_id = ?",
        (now, channel_id),
    )
    await db.commit()


async def get_ticket(db: aiosqlite.Connection, channel_id: int) -> dict[str, Any] | None:
    cur = await db.execute("SELECT * FROM open_tickets WHERE channel_id=?", (channel_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def count_user_open_tickets(
    db: aiosqlite.Connection, guild_id: int, owner_id: int
) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM open_tickets WHERE guild_id=? AND owner_id=?",
        (guild_id, owner_id),
    )
    row = await cur.fetchone()
    return int(row["c"] if row else 0)


# ─────────────────────────────────────────────────────────────
# BOT GUILDS (nur Server, auf denen der Phantom-Bot wirklich Member ist)
# ─────────────────────────────────────────────────────────────

async def sync_bot_guilds(
    db: aiosqlite.Connection, guilds: list[dict[str, Any]]
) -> int:
    """Replace the list of guilds the bot is actually in. Called by the bot."""
    now = int(time.time())
    await db.execute("DELETE FROM bot_guilds")
    for g in guilds:
        try:
            gid = int(g.get("id") or g.get("guild_id"))
        except (TypeError, ValueError):
            continue
        await db.execute(
            """
            INSERT INTO bot_guilds (guild_id, name, icon, member_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                gid,
                g.get("name"),
                g.get("icon"),
                g.get("member_count") or g.get("approximate_member_count"),
                now,
            ),
        )
    await db.commit()
    return len(guilds)


async def list_bot_guilds(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT * FROM bot_guilds ORDER BY name COLLATE NOCASE"
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_bot_guild(db: aiosqlite.Connection, guild_id: int) -> dict[str, Any] | None:
    cur = await db.execute("SELECT * FROM bot_guilds WHERE guild_id = ?", (guild_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def is_bot_in_guild(db: aiosqlite.Connection, guild_id: int) -> bool:
    cur = await db.execute("SELECT 1 FROM bot_guilds WHERE guild_id = ?", (guild_id,))
    return await cur.fetchone() is not None


async def update_bot_guild_stats(
    db: aiosqlite.Connection, guild_id: int, *, name: str | None = None, icon: str | None = None, member_count: int | None = None
) -> None:
    """Lightweight update for a single guild the bot is in."""
    now = int(time.time())
    sets = []
    params = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if icon is not None:
        sets.append("icon = ?")
        params.append(icon)
    if member_count is not None:
        sets.append("member_count = ?")
        params.append(member_count)
    sets.append("updated_at = ?")
    params.append(now)
    params.append(guild_id)

    if len(sets) > 1:
        await db.execute(
            f"UPDATE bot_guilds SET {', '.join(sets)} WHERE guild_id = ?",
            params,
        )
        await db.commit()


# ── LIVE STATS HELPERS (for main-bot-like overview) ──

async def get_phantom_stats(db: aiosqlite.Connection) -> dict[str, Any]:
    """Return live overview stats like the main University Bot dashboard."""
    cur = await db.execute("SELECT COUNT(*) FROM bot_guilds")
    total_servers = (await cur.fetchone())[0] or 0

    cur = await db.execute("SELECT COUNT(*) FROM open_tickets WHERE status = 'open'")
    open_tickets = (await cur.fetchone())[0] or 0

    cur = await db.execute("SELECT COUNT(*) FROM open_tickets WHERE status = 'claimed'")
    claimed_tickets = (await cur.fetchone())[0] or 0

    cur = await db.execute("SELECT COUNT(DISTINCT guild_id) FROM open_tickets")
    servers_with_tickets = (await cur.fetchone())[0] or 0

    # Recent activity (last 24h)
    day_ago = int(time.time()) - 86400
    cur = await db.execute(
        "SELECT COUNT(*) FROM open_tickets WHERE created_at > ? OR claimed_at > ?",
        (day_ago, day_ago),
    )
    recent_activity = (await cur.fetchone())[0] or 0

    return {
        "total_servers": total_servers,
        "open_tickets": open_tickets,
        "claimed_tickets": claimed_tickets,
        "servers_with_tickets": servers_with_tickets,
        "recent_activity": recent_activity,
        "total_tickets_active": open_tickets + claimed_tickets,
    }


async def get_guild_live_stats(db: aiosqlite.Connection, guild_id: int) -> dict[str, Any]:
    """Live stats for a single guild (like main bot overview)."""
    cur = await db.execute(
        "SELECT COUNT(*) FROM open_tickets WHERE guild_id = ? AND status = 'open'",
        (guild_id,),
    )
    open_count = (await cur.fetchone())[0] or 0

    cur = await db.execute(
        "SELECT COUNT(*) FROM open_tickets WHERE guild_id = ?",
        (guild_id,),
    )
    total_count = (await cur.fetchone())[0] or 0

    cur = await db.execute(
        "SELECT * FROM open_tickets WHERE guild_id = ? ORDER BY COALESCE(claimed_at, created_at) DESC LIMIT 5",
        (guild_id,),
    )
    recent = [dict(r) for r in await cur.fetchall()]

    return {
        "open_tickets": open_count,
        "total_tickets": total_count,
        "recent_tickets": recent,
    }
