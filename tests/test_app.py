from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from trade_m.app import build_app
from trade_m.config import Settings
from trade_m.domain import IST


def test_dashboard_and_unauthenticated_status(tmp_path: Path) -> None:
    settings = Settings(
        api_key="",
        api_secret="",
        redirect_url="http://127.0.0.1:8000/auth/callback",
        host="127.0.0.1",
        port=8000,
        database_path=tmp_path / "app.db",
        candle_finalization_delay_seconds=3,
    )
    app = build_app(settings)
    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Trade M" in dashboard.text
        assert "Add all constituents" in dashboard.text
        assert "Upload stocks from Excel" in dashboard.text
        assert "Download sample Excel" in dashboard.text
        assert "India VIX-based moving percentage" in dashboard.text
        assert "Sign in with Upstox" in dashboard.text
        assert "Sign in with Dhan" in dashboard.text

        status = client.get("/api/status").json()
        assert status["kite_configured"] is False
        assert status["authenticated"] is False
        assert status["monitor"]["connected"] is False
        assert status["providers"]["zerodha"]["configured"] is False
        assert status["providers"]["upstox"]["configured"] is False
        assert status["providers"]["dhan"]["configured"] is False

        assert client.get("/api/rules").json() == []
        assert client.get("/api/events").json() == []

        movement = client.get("/api/market-movement").json()
        assert movement["available"] is False
        assert movement["divisor"] == "8.54400"

        template = client.get("/static/trade_m_stock_import_template.xlsx")
        assert template.status_code == 200
        assert template.content.startswith(b"PK")


def test_market_movement_endpoint_uses_connected_upstox(tmp_path: Path) -> None:
    settings = Settings(
        api_key="",
        api_secret="",
        redirect_url="http://127.0.0.1:8000/auth/callback",
        host="127.0.0.1",
        port=8000,
        database_path=tmp_path / "app.db",
        candle_finalization_delay_seconds=3,
    )
    app = build_app(settings)
    gateway = app.state.gateways["upstox"]
    gateway.api_client = object()
    gateway.access_token = "test-token"
    gateway.india_vix_previous_close = lambda trading_date: (
        datetime.now(IST).date() - timedelta(days=1),
        Decimal("12.990"),
    )

    with TestClient(app) as client:
        movement = client.get("/api/market-movement").json()

    assert movement["available"] is True
    assert movement["provider"] == "upstox"
    assert movement["moving_percentage_display"] == "1.5203%"
