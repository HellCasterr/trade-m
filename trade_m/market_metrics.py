from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any


INDIA_VIX_DIVISOR = Decimal("8.54400")
MOVEMENT_PRECISION = Decimal("0.0001")


def calculate_moving_percentage(vix_close: Decimal) -> Decimal:
    """Return the configured daily movement value from an India VIX close."""
    close = Decimal(str(vix_close))
    if close <= 0:
        raise ValueError("India VIX close must be greater than zero.")
    # Product convention: retain four decimal places without rounding up
    # (12.990 / 8.54400 = 1.520365..., displayed as 1.5203).
    return (close / INDIA_VIX_DIVISOR).quantize(MOVEMENT_PRECISION, rounding=ROUND_DOWN)


class MarketMovementService:
    """Fetch and cache the previous India VIX close for the current trading day."""

    provider_order = ("upstox", "zerodha", "dhan")

    def __init__(self, retry_seconds: int = 60) -> None:
        self.retry_seconds = retry_seconds
        self._lock = threading.RLock()
        self._cache: dict[date, dict[str, Any]] = {}
        self._last_failure_at: datetime | None = None
        self._last_failure: str | None = None

    @staticmethod
    def _unavailable(message: str) -> dict[str, Any]:
        return {
            "available": False,
            "error": message,
            "formula": "India VIX previous close / 8.54400",
            "divisor": str(INDIA_VIX_DIVISOR),
        }

    def get(
        self,
        trading_date: date,
        gateways: dict[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        with self._lock:
            cached = self._cache.get(trading_date)
            if cached is not None:
                return dict(cached)

            authenticated = [
                name
                for name in self.provider_order
                if name in gateways and bool(gateways[name].authenticated)
            ]
            if not authenticated:
                return self._unavailable(
                    "Connect Upstox, Zerodha, or DhanHQ to calculate today's value."
                )

            if (
                self._last_failure_at is not None
                and now - self._last_failure_at < timedelta(seconds=self.retry_seconds)
            ):
                return self._unavailable(
                    self._last_failure or "India VIX data is temporarily unavailable."
                )

            errors: list[str] = []
            for provider in authenticated:
                gateway = gateways[provider]
                try:
                    reference_date, close = gateway.india_vix_previous_close(trading_date)
                    close = Decimal(str(close))
                    if reference_date >= trading_date:
                        raise ValueError(
                            "Broker returned a current-session value instead of the previous close."
                        )
                    movement = calculate_moving_percentage(close)
                    result = {
                        "available": True,
                        "provider": provider,
                        "reference_date": reference_date.isoformat(),
                        "vix_close": str(close),
                        "vix_close_display": f"{close:.3f}",
                        "moving_percentage": str(movement),
                        "moving_percentage_display": f"{movement:.4f}%",
                        "formula": "India VIX previous close / 8.54400",
                        "divisor": str(INDIA_VIX_DIVISOR),
                    }
                    self._cache[trading_date] = result
                    self._last_failure_at = None
                    self._last_failure = None
                    return dict(result)
                except Exception as exc:
                    errors.append(f"{provider.title()}: {exc}")

            self._last_failure_at = now
            self._last_failure = "Could not retrieve India VIX: " + "; ".join(errors)
            return self._unavailable(self._last_failure)
