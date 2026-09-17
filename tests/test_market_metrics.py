from datetime import date, datetime
from decimal import Decimal

from trade_m.domain import IST
from trade_m.market_metrics import MarketMovementService, calculate_moving_percentage


class FakeGateway:
    def __init__(self, *, authenticated: bool, result=None, error: Exception | None = None):
        self.authenticated = authenticated
        self.result = result
        self.error = error
        self.calls = 0

    def india_vix_previous_close(self, trading_date):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_moving_percentage_uses_configured_divisor_and_truncates_four_places() -> None:
    assert calculate_moving_percentage(Decimal("12.990")) == Decimal("1.5203")


def test_market_movement_uses_authenticated_provider_and_caches_daily_result() -> None:
    day = date(2026, 9, 17)
    now = datetime(2026, 9, 17, 9, 10, tzinfo=IST)
    gateway = FakeGateway(
        authenticated=True,
        result=(date(2026, 9, 16), Decimal("12.990")),
    )
    service = MarketMovementService()
    first = service.get(day, {"upstox": gateway}, now=now)
    second = service.get(day, {"upstox": gateway}, now=now)

    assert first["available"] is True
    assert first["moving_percentage"] == "1.5203"
    assert first["moving_percentage_display"] == "1.5203%"
    assert first["vix_close_display"] == "12.990"
    assert first["reference_date"] == "2026-09-16"
    assert second == first
    assert gateway.calls == 1


def test_market_movement_falls_back_to_another_connected_provider() -> None:
    day = date(2026, 9, 17)
    now = datetime(2026, 9, 17, 9, 10, tzinfo=IST)
    upstox = FakeGateway(authenticated=True, error=RuntimeError("temporary error"))
    zerodha = FakeGateway(
        authenticated=True,
        result=(date(2026, 9, 16), Decimal("12.990")),
    )
    result = MarketMovementService().get(
        day, {"upstox": upstox, "zerodha": zerodha}, now=now
    )
    assert result["available"] is True
    assert result["provider"] == "zerodha"


def test_market_movement_waits_for_provider_login() -> None:
    now = datetime(2026, 9, 17, 9, 10, tzinfo=IST)
    result = MarketMovementService().get(
        now.date(), {"upstox": FakeGateway(authenticated=False)}, now=now
    )
    assert result["available"] is False
    assert "Connect" in result["error"]
