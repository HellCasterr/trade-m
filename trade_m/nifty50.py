from __future__ import annotations

import csv
import io
import threading
import time
from dataclasses import dataclass
from urllib.request import Request, urlopen


CONSTITUENTS_URL = (
    "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
)

# Used only when the official CSV is temporarily unavailable. The dashboard labels the source.
FALLBACK_SYMBOLS = (
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR",
    "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
)


@dataclass(frozen=True)
class NiftyConstituents:
    symbols: tuple[str, ...]
    source: str


class Nifty50Service:
    def __init__(self, cache_seconds: int = 43_200) -> None:
        self.cache_seconds = cache_seconds
        self._cached: NiftyConstituents | None = None
        self._cached_at = 0.0
        self._lock = threading.RLock()

    def get(self, force_refresh: bool = False) -> NiftyConstituents:
        with self._lock:
            if (
                not force_refresh
                and self._cached is not None
                and time.monotonic() - self._cached_at < self.cache_seconds
            ):
                return self._cached
        try:
            result = NiftyConstituents(self._download(), "NSE Indices")
        except Exception:
            result = NiftyConstituents(FALLBACK_SYMBOLS, "packaged fallback")
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result

    @staticmethod
    def _download() -> tuple[str, ...]:
        request = Request(
            CONSTITUENTS_URL,
            headers={
                "User-Agent": "Mozilla/5.0 Trade-M/0.2",
                "Accept": "text/csv,*/*",
            },
        )
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8-sig")
        rows = csv.DictReader(io.StringIO(body))
        symbols = tuple(
            dict.fromkeys(
                str(row.get("Symbol", "")).strip().upper()
                for row in rows
                if str(row.get("Symbol", "")).strip()
            )
        )
        if len(symbols) != 50:
            raise ValueError(f"Expected 50 Nifty constituents, received {len(symbols)}")
        return symbols
