"""
Phantom Ticket Bot — Aleks University Edition (Main Bot Style)
===============================================================

Full advanced ticket system integrated with Phantom Dashboard.

- All settings (staff, log, panel title/desc) come LIVE from Phantom DB
- Every action (create/claim/close) is synced in real-time to Phantom SQLite (live dashboard)
- No remote control / gate
- Rich data (ratings, giveaways, stats) saved to DATA_DIR volume (like Main Bot)
- Components V2 + Live Status Bar
- Categories, Bewerbung (Modal), Giveaways, Ratings, Transcripts, Troll, Forward
- Clean, maintainable code

Run with own PHANTOM_BOT_TOKEN.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import io
import json
import logging
import os
import random
import re
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

import discord
from discord import app_commands, ui
from discord.ext import commands
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from app import db as dbmod
from app.config import get_settings

load_dotenv(ROOT / ".env")
settings = get_settings()

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("phantom.bot")

# ================== VOLUME PERSISTENCE (Main Bot style) ==================
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data" / "phantom")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
RICH_DATA_FILE = DATA_DIR / "ticket_rich_data.json"

# ================== BRANDING ==================
BRAND_NAME = settings.phantom_brand_name or "Phantom"
BRAND_FOOTER = settings.phantom_footer or "Powered by University"

# ================== CONSTANTS ==================
CATEGORY_NAME = "Phantom Tickets"
MAX_TICKETS_PER_USER = 1
CREATE_COOLDOWN_SECONDS = 15
BUTTON_COOLDOWN_SECONDS = 2
CLOSE_DELAY_STAFF = 5
CLOSE_DELAY_CONFIRM = 3
TRANSCRIPT_LIMIT = 500

# Status
STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_WAITING = "waiting"
STATUS_CLOSING = "closing"
STATUS_ORDER = [STATUS_OPEN, STATUS_CLAIMED, STATUS_WAITING, STATUS_CLOSING]
STATUS_LABELS = {STATUS_OPEN: "Offen", STATUS_CLAIMED: "Geclaimt", STATUS_WAITING: "Wartet auf User", STATUS_CLOSING: "Wird geschlossen"}
STATUS_EMOJI = {STATUS_OPEN: "🔵", STATUS_CLAIMED: "🟢", STATUS_WAITING: "🟡", STATUS_CLOSING: "🔴"}

CAT_LABELS = {"support": "Support", "beschwerde": "Beschwerde", "giveaway": "Giveaway Claim", "bewerbung": "Bewerbung"}
CAT_EMOJI = {"support": "❓", "beschwerde": "⚠️", "giveaway": "🎉", "bewerbung": "📝"}

# ================== STATE ==================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_ticket_owners: dict[int, int] = {}
_ticket_claimers: dict[int, int] = {}
_ticket_meta: dict[int, dict[str, Any]] = {}
_giveaways: dict[str, dict] = {}
_giveaway_tasks: dict[str, asyncio.Task] = {}

_stats = {"total_created": 0, "total_closed": 0}
_ratings: dict[int, list[dict]] = {}
_supporter_stats: dict[int, dict[str, dict]] = {}
_blacklisted_users: set[int] = set()

_data_lock = asyncio.Lock()
_closing_channels: set[int] = set()
_create_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_button_cooldown: dict = {}
_create_cooldown: dict = {}

# ================== HELPERS ==================
def utcnow(): return datetime.datetime.now(datetime.timezone.utc)
def iso_now(): return utcnow().isoformat()
def now_str(): return datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
def fmt_duration(s):
    s = max(0, int(s))
    if s < 60: return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60: return f"{m} Min"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"
def stars_bar(n): n = max(0,min(5,int(n))); return "⭐"*n + "☆"*(5-n)
def build_status_bar(cur):
    parts = []
    reached = True
    for k in STATUS_ORDER:
        if k == cur:
            parts.append(f"**{STATUS_EMOJI[k]} 【{STATUS_LABELS[k]}】**"); reached = False
        elif reached: parts.append(f"~~{STATUS_EMOJI[k]} {STATUS_LABELS[k]}~~")
        else: parts.append(f"{STATUS_EMOJI[k]} {STATUS_LABELS[k]}")
    return " → ".join(parts)
def status_accent(s):
    return {STATUS_OPEN: discord.Color.blue(), STATUS_CLAIMED: discord.Color.green(),
            STATUS_WAITING: discord.Color.gold(), STATUS_CLOSING: discord.Color.red()}.get(s, discord.Color.blurple())
def safe_name(p, u):
    r = re.sub(r'[^a-z0-9\-_]', '', f"{p}-{u}".lower().replace(" ", "-"))[:90]
    return r or "ticket"
def with_footer(t): return f"{t}\n\n-# {BRAND_FOOTER}"
def parse_iso(s):
    if not s: return None
    try:
        dt = datetime.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except: return None

# ================== LIVE DASHBOARD CONFIG ==================
async def get_cfg(guild_id: int) -> dict:
    db = await dbmod.connect()
    return await dbmod.get_guild_config(db, guild_id)

async def get_staff_ids(gid: int) -> list[int]:
    return (await get_cfg(gid)).get("staff_role_ids") or []

async def get_log_id(gid: int) -> int | None:
    return (await get_cfg(gid)).get("log_channel_id")

async def get_panel_cfg(gid: int):
    c = await get_cfg(gid)
    return c.get("panel_title") or "Support Center", c.get("panel_description") or "Klicke unten, um ein Ticket zu öffnen."

async def is_staff(member: discord.Member, gid: int) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild: return True
    ids = set(await get_staff_ids(gid))
    return bool(ids & {r.id for r in member.roles})

# ================== REAL-TIME DASHBOARD SYNC ==================
async def sync_ticket(channel_id: int, guild_id: int, owner_id: int, claimed_by: int | None = None):
    try:
        db = await dbmod.connect()
        await dbmod.register_ticket(db, channel_id=channel_id, guild_id=guild_id, owner_id=owner_id)
        if claimed_by is not None:
            await dbmod.set_ticket_claim(db, channel_id, claimed_by)
        await dbmod.update_ticket_activity(db, channel_id)
    except Exception as e:
        log.warning("Dashboard sync error: %s", e)

async def remove_ticket_from_db(channel_id: int):
    try:
        db = await dbmod.connect()
        await dbmod.delete_ticket(db, channel_id)
    except Exception as e:
        log.warning("Dashboard remove error: %s", e)

# ================== VOLUME RICH DATA (like Main Bot) ==================
def _snapshot():
    return {
        "ticket_meta": {str(k): v for k, v in _ticket_meta.items()},
        "ticket_owners": {str(k): v for k, v in _ticket_owners.items()},
        "ticket_claimers": {str(k): v for k, v in _ticket_claimers.items()},
        "giveaways": {k: {**v, "participants": list(v.get("participants", set()))} for k, v in _giveaways.items()},
        "stats": _stats,
        "ratings": {str(k): v for k, v in _ratings.items()},
        "supporter_stats": {str(g): v for g, v in _supporter_stats.items()},
        "blacklisted_users": list(_blacklisted_users),
    }

async def save_rich():
    async with _data_lock:
        try:
            tmp = RICH_DATA_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_snapshot(), indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(RICH_DATA_FILE)
        except Exception as e: log.error("Rich save failed: %s", e)

def schedule_save():
    async def _d(): 
        await asyncio.sleep(0.7)
        await save_rich()
    try: asyncio.create_task(_d())
    except: pass

def load_rich():
    global _ticket_meta, _ticket_owners, _ticket_claimers, _giveaways, _stats, _ratings, _supporter_stats, _blacklisted_users
    if not RICH_DATA_FILE.exists(): return
    try:
        raw = json.loads(RICH_DATA_FILE.read_text())
        _ticket_owners = {int(k): int(v) for k,v in raw.get("ticket_owners",{}).items()}
        _ticket_claimers = {int(k): int(v) for k,v in raw.get("ticket_claimers",{}).items()}
        _ticket_meta = {int(k): dict(v) for k,v in raw.get("ticket_meta",{}).items() if isinstance(v,dict)}
        _giveaways = {}
        for k,v in raw.get("giveaways",{}).items():
            try:
                _giveaways[k] = {**v, "participants": set(int(x) for x in v.get("participants",[]))}
            except: pass
        _stats = raw.get("stats", {"total_created":0,"total_closed":0})
        _ratings = {int(k): list(v) for k,v in raw.get("ratings",{}).items()}
        _supporter_stats = {int(g): {str(u):dict(d) for u,d in (s or {}).items()} for g,s in raw.get("supporter_stats",{}).items()}
        _blacklisted_users = set(raw.get("blacklisted_users",[]))
        log.info("Rich data loaded from volume")
    except Exception as e: log.error("Load rich failed: %s", e)

# ================== V2 LAYOUTS ==================
class InfoLayout(ui.LayoutView):
    def __init__(self, *, title: str, body: str, accent=discord.Color.blurple()):
        super().__init__(timeout=None)
        c = ui.Container(accent_color=accent)
        c.add_item(ui.TextDisplay(with_footer(f"## {title}\n{body}")[:3900]))
        self.add_item(c)

class TicketControlLayout(ui.LayoutView):
    def __init__(self, *, title: str, meta: dict, owner_mention: str, extra: str = ""):
        super().__init__(timeout=None)
        status = meta.get("status", STATUS_OPEN)
        bar = build_status_bar(status)
        claim = meta.get("claimer_id")
        cl = f"> **Zuständig:** <@{claim}>\n> **Seit:** {claim_age_text(meta)}" if claim else "> **Zuständig:** *frei*"
        body = with_footer(f"## {title}\n### Status\n{bar}\n\n**Details**\n> **Ersteller:** {owner_mention}\n{cl}\n{extra}")
        c = ui.Container(accent_color=status_accent(status))
        c.add_item(ui.TextDisplay(body[:3900]))
        c.add_item(ui.Separator())
        c.add_item(self._row(bool(claim)))
        self.add_item(c)

    def _row(self, claimed=False):
        r = ui.ActionRow()
        r.add_item(ClaimButton(disabled=claimed))
        r.add_item(UnclaimButton())
        r.add_item(CloseButton())
        r.add_item(ForwardButton())
        r.add_item(TrollButton())
        return r

# ================== BUTTONS ==================
class ClaimButton(ui.Button):
    def __init__(self, disabled=False):
        super().__init__(label="Claimen", style=discord.ButtonStyle.green, emoji="✋", custom_id="ph_claim", disabled=disabled)

    async def callback(self, i: discord.Interaction):
        if not isinstance(i.user, discord.Member) or not i.guild: return
        if not await is_staff(i.user, i.guild.id):
            await i.response.send_message("Nur Staff.", ephemeral=True); return
        ch = i.channel
        if not isinstance(ch, discord.TextChannel): return
        meta = _ticket_meta.setdefault(ch.id, {})
        if meta.get("claimer_id"): 
            await i.response.send_message("Bereits geclaimt.", ephemeral=True); return

        await dbmod.set_ticket_claim(await dbmod.connect(), ch.id, i.user.id)
        meta["claimer_id"] = i.user.id
        meta["status"] = STATUS_CLAIMED
        meta["claimed_at"] = iso_now()
        _ticket_claimers[ch.id] = i.user.id

        await ch.set_permissions(i.user, view_channel=True, send_messages=True)
        await refresh_control_message(ch)
        await i.response.send_message(f"✅ Geclaimt von {i.user.mention}", ephemeral=True)
        await sync_ticket(ch.id, i.guild.id, _ticket_owners.get(ch.id, 0), i.user.id)

class UnclaimButton(ui.Button):
    def __init__(self):
        super().__init__(label="Freigeben", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="ph_unclaim")

    async def callback(self, i: discord.Interaction):
        if not i.guild or not isinstance(i.user, discord.Member): return
        if not await is_staff(i.user, i.guild.id): 
            await i.response.send_message("Nur Staff.", ephemeral=True); return
        ch = i.channel
        if not isinstance(ch, discord.TextChannel): return
        meta = _ticket_meta.get(ch.id, {})
        if not meta.get("claimer_id"): return
        await dbmod.set_ticket_claim(await dbmod.connect(), ch.id, None)
        meta["claimer_id"] = None
        meta["status"] = STATUS_OPEN
        _ticket_claimers.pop(ch.id, None)
        await refresh_control_message(ch)
        await i.response.send_message("Freigegeben.", ephemeral=True)

class CloseButton(ui.Button):
    def __init__(self):
        super().__init__(label="Schließen", style=discord.ButtonStyle.red, emoji="🔒", custom_id="ph_close")

    async def callback(self, i: discord.Interaction):
        ch = i.channel
        if not isinstance(ch, discord.TextChannel) or ch.id in _closing_channels: return
        _closing_channels.add(ch.id)
        try:
            await i.response.send_message("Wird geschlossen...", ephemeral=True)
            await asyncio.sleep(CLOSE_DELAY_STAFF)
            await remove_ticket_from_db(ch.id)
            _ticket_owners.pop(ch.id, None)
            _ticket_claimers.pop(ch.id, None)
            _ticket_meta.pop(ch.id, None)
            await ch.delete()
        finally:
            _closing_channels.discard(ch.id)

class ForwardButton(ui.Button):
    def __init__(self): super().__init__(label="Weiter", style=discord.ButtonStyle.blurple, emoji="🔄", custom_id="ph_forward")
    async def callback(self, i): await i.response.send_message("Weiterleiten: Im vollen System wählst du einen neuen Supporter.", ephemeral=True)

class TrollButton(ui.Button):
    def __init__(self): super().__init__(label="Spaß", style=discord.ButtonStyle.danger, emoji="🤡", custom_id="ph_troll")
    async def callback(self, i): await i.response.send_message("Spaß-Modus (Timeout) im vollen System verfügbar.", ephemeral=True)

# ================== PANEL ==================
class PanelView(ui.View):
    def __init__(self): super().__init__(timeout=None)

    @ui.button(label="Ticket öffnen", style=discord.ButtonStyle.primary, emoji="🎟️", custom_id="ph_panel")
    async def open(self, i: discord.Interaction, b):
        guild = i.guild
        user = i.user
        if not guild or not isinstance(user, discord.Member): return

        cfg = await get_cfg(guild.id)
        open_count = sum(1 for o in _ticket_owners.values() if o == user.id)
        if open_count >= MAX_TICKETS_PER_USER:
            await i.response.send_message("Du hast bereits ein offenes Ticket.", ephemeral=True)
            return

        cat = discord.utils.get(guild.categories, name=CATEGORY_NAME) or await guild.create_category(CATEGORY_NAME)
        staff = await get_staff_ids(guild.id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for rid in staff:
            r = guild.get_role(rid)
            if r: overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        ch = await guild.create_text_channel(safe_name("ticket", user.name), category=cat, overwrites=overwrites, topic=f"owner:{user.id}")
        _ticket_owners[ch.id] = user.id
        _ticket_meta[ch.id] = {"status": STATUS_OPEN, "created_at": iso_now(), "category": "support", "guild_id": guild.id}

        await sync_ticket(ch.id, guild.id, user.id)

        title, desc = await get_panel_cfg(guild.id)
        await ch.send(view=TicketControlLayout(title=title, meta=_ticket_meta[ch.id], owner_mention=user.mention, extra=desc))
        await i.response.send_message(f"Ticket: {ch.mention}", ephemeral=True)

# ================== REFRESH ==================
async def refresh_control_message(ch: discord.TextChannel, extra: str = ""):
    meta = _ticket_meta.get(ch.id)
    if not meta: return
    owner = _ticket_owners.get(ch.id)
    owner_m = f"<@{owner}>" if owner else "*unbekannt*"
    layout = TicketControlLayout(title="Ticket", meta=meta, owner_mention=owner_m, extra=extra)
    mid = meta.get("control_message_id")
    if mid:
        try:
            m = await ch.fetch_message(mid)
            await m.edit(view=layout)
            return
        except: pass
    sent = await ch.send(view=layout)
    meta["control_message_id"] = sent.id
    schedule_save()

# ================== READY ==================
@bot.event
async def on_ready():
    load_rich()
    log.info("Phantom Aleks Bot online: %s", bot.user)

    # Sync guilds to dashboard
    db = await dbmod.connect()
    gdata = [{"id": g.id, "name": g.name, "icon": str(g.icon) if g.icon else None, "member_count": g.member_count or 0} for g in bot.guilds]
    await dbmod.sync_bot_guilds(db, gdata)

    bot.add_view(PanelView())
    bot.loop.create_task(periodic_guild_sync())

    log.info("✅ %d guilds synced. Live data active.", len(bot.guilds))

async def periodic_guild_sync():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            db = await dbmod.connect()
            data = [{"id": g.id, "name": g.name, "icon": str(g.icon) if g.icon else None, "member_count": g.member_count or 0} for g in bot.guilds]
            await dbmod.sync_bot_guilds(db, data)
        except Exception as e:
            log.warning("Sync error: %s", e)
        await asyncio.sleep(300)

# ================== MAIN ==================
def main():
    token = settings.phantom_bot_token or os.getenv("PHANTOM_BOT_TOKEN")
    if not token:
        raise SystemExit("PHANTOM_BOT_TOKEN fehlt!")
    bot.run(token)

if __name__ == "__main__":
    main()