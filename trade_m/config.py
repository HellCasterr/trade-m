from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    redirect_url: str
    host: str
    port: int
    database_path: Path
    candle_finalization_delay_seconds: int
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_redirect_url: str = "http://127.0.0.1:8000/auth/upstox/callback"
    dhan_client_id: str = ""
    dhan_api_key: str = ""
    dhan_api_secret: str = ""
    dhan_access_token: str = ""

    @property
    def kite_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def upstox_configured(self) -> bool:
        return bool(self.upstox_api_key and self.upstox_api_secret)

    @property
    def dhan_configured(self) -> bool:
        return bool(
            self.dhan_client_id
            and (
                self.dhan_access_token
                or (self.dhan_api_key and self.dhan_api_secret)
            )
        )


def get_settings() -> Settings:
    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        api_key=os.getenv("KITE_API_KEY", "").strip(),
        api_secret=os.getenv("KITE_API_SECRET", "").strip(),
        redirect_url=os.getenv(
            "KITE_REDIRECT_URL", "http://127.0.0.1:8000/auth/callback"
        ).strip(),
        host=os.getenv("APP_HOST", "127.0.0.1").strip(),
        port=int(os.getenv("APP_PORT", "8000")),
        database_path=Path(os.getenv("DATABASE_PATH", str(data_dir / "trade_m.db"))),
        candle_finalization_delay_seconds=max(
            0, int(os.getenv("CANDLE_FINALIZATION_DELAY_SECONDS", "3"))
        ),
        upstox_api_key=os.getenv("UPSTOX_API_KEY", "").strip(),
        upstox_api_secret=os.getenv("UPSTOX_API_SECRET", "").strip(),
        upstox_redirect_url=os.getenv(
            "UPSTOX_REDIRECT_URL", "http://127.0.0.1:8000/auth/upstox/callback"
        ).strip(),
        dhan_client_id=os.getenv("DHAN_CLIENT_ID", "").strip(),
        dhan_api_key=os.getenv("DHAN_API_KEY", "").strip(),
        dhan_api_secret=os.getenv("DHAN_API_SECRET", "").strip(),
        dhan_access_token=os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
    )
