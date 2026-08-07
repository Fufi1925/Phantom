# Phantom — isoliertes Ticket-Dashboard

Komplett **eigenes** System unter:

```text
{deine-domain}/phantom/dashboard
{deine-domain}/phantom/login
{deine-domain}/phantom/api/...
```

- **Kein** University-Bot / Oskar-Bot Import  
- **Eigener** Discord-Bot-Token  
- **Eigenes** Discord-OAuth (Login)  
- **Eigene** SQLite-DB unter `phantom/data/`  
- **Kein** Remote-Control / Killswitch  
- Cookies nur auf Path `/phantom`

---

## Struktur

```text
phantom/
├── app/                 # FastAPI Dashboard + API
│   ├── main.py
│   ├── auth.py
│   ├── config.py
│   ├── db.py
│   ├── templates/       # Login + Dashboard UI
│   └── static/css/
├── bot/
│   └── ticket_bot.py    # Nur Tickets
├── data/                # SQLite (lokal)
├── .env.example
├── requirements.txt
├── run_dashboard.py
├── run_bot.py
└── nginx.phantom.conf.example
```

---

## Setup

### 1) Python deps

```bash
cd phantom
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 2) Discord Application (NEU)

1. https://discord.com/developers/applications → **New Application** (z.B. „Phantom Tickets“)
2. **Bot** → Add Bot → Token kopieren → `PHANTOM_BOT_TOKEN`
3. Bot Intents: Presence optional, **Server Members**, **Message Content**
4. **OAuth2** → Client ID + Secret → `PHANTOM_DISCORD_CLIENT_ID` / `PHANTOM_DISCORD_CLIENT_SECRET`
5. Redirects hinzufügen:
   ```text
   https://DEINE-DOMAIN.de/phantom/auth/callback
   ```
6. Bot einladen (Scopes: `bot applications.commands`, Rechte: Channels verwalten, Nachrichten, Rollen lesen …)

### 3) `.env` füllen

```env
PHANTOM_BASE_URL=https://DEINE-DOMAIN.de/phantom
PHANTOM_SECRET_KEY=irgendein-langes-zufaelliges-secret
PHANTOM_DISCORD_CLIENT_ID=...
PHANTOM_DISCORD_CLIENT_SECRET=...
PHANTOM_BOT_TOKEN=...
PHANTOM_BOT_OWNER_IDS=deine_user_id
```

### 4) Starten

Terminal A — Dashboard + API:

```bash
python run_dashboard.py
```

Terminal B — Ticket-Bot:

```bash
python run_bot.py
```

Lokal öffnen:

```text
http://127.0.0.1:8787/login
```

> Wenn du **ohne** Reverse-Proxy testest: App nutzt `root_path` aus `PHANTOM_BASE_URL`.
> Am einfachsten lokal `PHANTOM_BASE_URL=http://127.0.0.1:8787` setzen und
> OAuth-Callback `http://127.0.0.1:8787/auth/callback` — **oder** Nginx wie unten.

### 5) Production unter `/phantom`

- `PHANTOM_BASE_URL=https://deine-domain.de/phantom`
- Nginx: siehe `nginx.phantom.conf.example`
- OAuth Redirect: `https://deine-domain.de/phantom/auth/callback`

---

## Variablen (Namen)

| Variable | Zweck |
|----------|--------|
| `PHANTOM_BASE_URL` | Öffentliche Basis inkl. `/phantom` |
| `PHANTOM_SECRET_KEY` | Session-Signatur |
| `PHANTOM_COOKIE_NAME` | Cookie-Name (default `phantom_session`) |
| `PHANTOM_COOKIE_PATH` | Cookie-Path (default `/phantom`) |
| `PHANTOM_DISCORD_CLIENT_ID` | OAuth App |
| `PHANTOM_DISCORD_CLIENT_SECRET` | OAuth Secret |
| `PHANTOM_BOT_TOKEN` | Ticket-Bot Token |
| `PHANTOM_BOT_OWNER_IDS` | Owner IDs |
| `PHANTOM_DB_PATH` | SQLite-Pfad |
| `PHANTOM_BRAND_NAME` | Anzeigename |
| `PHANTOM_FOOTER` | Footer-Text |

---

## API (nur Phantom)

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/health` | public |
| GET | `/api/me` | Session-Cookie |
| GET | `/api/guilds/{id}/config` | Session oder Bot-Header |
| GET | `/api/guilds/{id}/tickets` | Session oder Bot-Header |
| GET | `/api/internal/guilds/{id}/config` | Header `X-Phantom-Bot-Token` |
| POST | `/api/internal/tickets/register` | Bot-Header |
| POST | `/api/internal/tickets/{channel_id}/claim` | Bot-Header |
| DELETE | `/api/internal/tickets/{channel_id}` | Bot-Header |

Bot-Header:

```http
X-Phantom-Bot-Token: <PHANTOM_BOT_TOKEN>
```

---

## Bot-Commands (Discord)

| Command | Beschreibung |
|---------|----------------|
| `!panel` | Ticket-Panel posten (Admin) |
| `!setstaff @Rolle…` | Staff-Rollen speichern |
| `!setlog` | Aktuellen Kanal als Log setzen |

Buttons: **Ticket erstellen** · **Claimen** · **Schließen**

---

## OAuth Scopes

Phantom fordert automatisch:

- `identify`
- `guilds`
- `guilds.join`

Server-Liste wird nach Login in SQLite (`user_guilds`) gespeichert und ist lesbar über:

- Dashboard Server-Auswahl
- `GET /api/me/guilds`
- `GET /api/me/guilds?refresh=1` (live neu laden + speichern)

## Isolation Checkliste

- [ ] Eigene Discord Application (nicht University)
- [ ] Eigener Bot-Token
- [ ] Eigene OAuth Redirects nur `/phantom/...`
- [ ] Eigene SQLite unter `phantom/data/`
- [ ] Cookie Path = `/phantom`
- [ ] Kein Import aus `Oskar-Bot/`
- [ ] Nginx mounted nur `/phantom/` auf diesen Service

---

## Design

Login/Dashboard im dunklen „Discord-nahen“ Stil (Card, Blur, Gradient) — eigenes CSS, kein Code vom University-Dashboard.
