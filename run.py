from __future__ import annotations

import logging
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from trade_m.config import get_settings


if __name__ == "__main__":
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_dir / "trade_m.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO, handlers=[console_handler, file_handler], force=True
    )
    settings = get_settings()
    url = f"http://{settings.host}:{settings.port}"
    logging.getLogger("trade_m").info(
        "Trade M starting at %s (logs: %s)", url, log_dir / "trade_m.log"
    )
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "trade_m.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
        log_config=None,
    )
