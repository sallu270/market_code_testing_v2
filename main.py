"""
Entrypoint to run Upstox NIFTY Phase 1 Data Collector from root workspace.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PHASE1_DIR = BASE_DIR / "upstox-nifty-phase1"
sys.path.insert(0, str(PHASE1_DIR))

if __name__ == "__main__":
    import main as phase1_main
    from config import load_settings

    settings = load_settings()
    print("=" * 68)
    print("Upstox NIFTY Phase 1 — live data collector")
    print(f"Dashboard: http://{settings.host}:{settings.port}")
    print(f"Database:  {settings.db_path}")
    print(f"Configured: {'yes' if settings.configured else 'no'}")
    print("=" * 68)

    phase1_main.app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        threaded=True,
        use_reloader=False,
    )