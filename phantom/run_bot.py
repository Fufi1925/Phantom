"""Start Phantom Ticket-Bot only."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from bot.ticket_bot import main

if __name__ == "__main__":
    main()
