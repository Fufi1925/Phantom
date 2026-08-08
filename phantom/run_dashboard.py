"""Start Phantom Dashboard+API.

Routes sind unter dem Path-Prefix aus PHANTOM_BASE_URL erreichbar
(z.B. /phantom/login), auch ohne Nginx.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.main import create_app


def build() -> FastAPI:
    s = get_settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    inner = create_app()

    # --- EINFACHES ADMIN-PANEL NUR FÜR DEN OWNER ---
    # Trage hier deine echte Discord-ID als String ein:
    OWNER_DISCORD_ID = "1523728380476919910"  # <-- Hier ID einfügen

    @inner.get("/admin-panel")
    async def owner_admin_panel(request: Request):
        user = request.session.get("user")
        if not user:
            return RedirectResponse(url="/phantom/login")
        
        if str(user.get("id")) != OWNER_DISCORD_ID:
            raise HTTPException(status_code=403, detail="Zugriff verweigert. Nur für den Owner!")
        
        return HTMLResponse("<h1>Willkommen im Admin Panel, Chef!</h1>")
    # ----------------------------------------------

    prefix = s.root_path  # e.g. "/phantom" or ""
    if prefix:
        outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        outer.mount(prefix, inner)
        # convenience redirect
        @outer.get("/")
        async def _root():
            return RedirectResponse(url=prefix + "/login")

        return outer
    return inner


app = build()


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "run_dashboard:app",
        host=s.phantom_host,
        port=int(s.phantom_port),
        reload=os.getenv("PHANTOM_RELOAD", "0") == "1",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
