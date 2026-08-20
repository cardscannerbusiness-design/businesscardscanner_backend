#!/usr/bin/env python
"""Run the CardSync FastAPI backend."""
import os
from pathlib import Path

import uvicorn

from utils.env_loader import load_env

BACKEND_ROOT = Path(__file__).resolve().parent


def main() -> None:
    os.chdir(BACKEND_ROOT)
    load_env()

    host = os.environ.get("HOST") or "0.0.0.0"
    port = int(os.environ.get("PORT") or "5000")
    # Default off: file-watch reload restarts the API on log/temp writes and
    # looks like the app is crashing. Set RELOAD=true for local development.
    reload = os.environ.get("RELOAD", "false").lower() in {"1", "true", "yes"}

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
