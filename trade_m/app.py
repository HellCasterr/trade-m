from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, get_settings
from .domain import IST, SESSION_CLOSE, SESSION_OPEN
from .kite_service import KiteGateway, KiteUnavailable, LiveMonitor, create_daily_rule
from .storage import Store


class RuleInput(BaseModel):
    exchange: str
    tradingsymbol: str
    percentage: Decimal


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.database_path)
    gateway = KiteGateway(settings.api_key, settings.api_secret)
    monitor = LiveMonitor(
        api_key=settings.api_key,
        store=store,
        finalization_delay_seconds=settings.candle_finalization_delay_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        monitor.stop()

    app = FastAPI(
        title="Trade M", version="0.1.0", docs_url="/api/docs", lifespan=lifespan
    )
    app.state.settings = settings
    app.state.store = store
    app.state.gateway = gateway
    app.state.monitor = monitor

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/auth/login", include_in_schema=False)
    def login() -> RedirectResponse:
        if not settings.kite_configured:
            return RedirectResponse("/?error=" + quote("Add your Kite API key and secret to .env first."))
        try:
            return RedirectResponse(gateway.login_url())
        except KiteUnavailable as exc:
            return RedirectResponse("/?error=" + quote(str(exc)))

    @app.get("/auth/callback", include_in_schema=False)
    def auth_callback(
        request_token: str | None = None,
        status: str | None = None,
        action: str | None = None,
    ) -> RedirectResponse:
        if status == "cancelled" or action == "logout":
            return RedirectResponse("/?error=" + quote("Zerodha login was cancelled."))
        if not request_token:
            return RedirectResponse("/?error=" + quote("Zerodha did not return a request token."))
        try:
            gateway.authenticate(request_token)
            monitor.start(gateway.access_token or "")
        except Exception as exc:
            return RedirectResponse("/?error=" + quote(f"Zerodha login failed: {exc}"))
        return RedirectResponse("/?login=success")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        now = datetime.now(IST)
        market_open = (
            now.weekday() < 5
            and SESSION_OPEN <= now.time().replace(tzinfo=None) < SESSION_CLOSE
        )
        return {
            "kite_configured": settings.kite_configured,
            "authenticated": gateway.authenticated,
            "user_name": gateway.user_name,
            "market_open": market_open,
            "server_time": now.isoformat(),
            "monitor": monitor.status(),
        }

    @app.get("/api/instruments/search")
    def search_instruments(
        q: str = Query(min_length=1, max_length=40),
        exchange: str = Query(default="NSE", pattern="^(NSE|BSE)$"),
    ) -> list[dict[str, Any]]:
        try:
            return gateway.search_instruments(q, exchange)
        except KiteUnavailable as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Instrument lookup failed: {exc}") from exc

    @app.get("/api/rules")
    def list_rules() -> list[dict[str, Any]]:
        return store.active_rules(datetime.now(IST).date())

    @app.post("/api/rules", status_code=201)
    def add_rule(payload: RuleInput) -> dict[str, Any]:
        exchange = payload.exchange.strip().upper()
        symbol = payload.tradingsymbol.strip().upper()
        if exchange not in {"NSE", "BSE"}:
            raise HTTPException(status_code=400, detail="Exchange must be NSE or BSE.")
        if not symbol:
            raise HTTPException(status_code=400, detail="Trading symbol is required.")
        try:
            percentage = Decimal(str(payload.percentage))
        except InvalidOperation as exc:
            raise HTTPException(status_code=400, detail="Enter a valid percentage.") from exc
        if percentage <= 0 or percentage > 50:
            raise HTTPException(status_code=400, detail="Percentage must be greater than 0 and at most 50.")
        try:
            rule = create_daily_rule(
                gateway,
                store,
                exchange=exchange,
                tradingsymbol=symbol,
                percentage=percentage,
                trading_date=datetime.now(IST).date(),
            )
            monitor.subscribe([int(rule["instrument_token"])])
            return rule
        except KiteUnavailable as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not create the rule: {exc}") from exc

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int) -> dict[str, bool]:
        if not store.deactivate_rule(rule_id):
            raise HTTPException(status_code=404, detail="Rule not found.")
        return {"deleted": True}

    @app.get("/api/events")
    def events(
        after_id: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        return store.events_after(after_id, limit)

    return app


app = build_app()
