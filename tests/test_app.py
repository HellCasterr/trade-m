from pathlib import Path

from fastapi.testclient import TestClient

from trade_m.app import build_app
from trade_m.config import Settings


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

        status = client.get("/api/status").json()
        assert status["kite_configured"] is False
        assert status["authenticated"] is False
        assert status["monitor"]["connected"] is False

        assert client.get("/api/rules").json() == []
        assert client.get("/api/events").json() == []
