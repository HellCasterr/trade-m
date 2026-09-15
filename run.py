from __future__ import annotations

import threading
import webbrowser

import uvicorn

from trade_m.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    url = f"http://{settings.host}:{settings.port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(
        "trade_m.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
