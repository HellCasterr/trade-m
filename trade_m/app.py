from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .dhan_service import DhanGateway, DhanMonitor
from .domain import IST, SESSION_CLOSE, SESSION_OPEN
from .excel_import import StockWorkbookError, parse_stock_workbook
from .kite_service import KiteGateway, KiteUnavailable, LiveMonitor, create_daily_rule
from .market_metrics import MarketMovementService
from .nifty50 import Nifty50Service
from .storage import Store
from .upstox_service import UpstoxGateway, UpstoxMonitor


class RuleInput(BaseModel):
    provider: str = "zerodha"
    exchange: str
    tradingsymbol: str
    percentage: Decimal


class RulePatch(BaseModel):
    percentage: Decimal | None = None
    active: bool | None = None


class NiftyBulkInput(BaseModel):
    provider: str = "zerodha"
    common_percentage: Decimal
    percentages: dict[str, Decimal] = Field(default_factory=dict)


class BulkStatusInput(BaseModel):
    active: bool
    rule_ids: list[int] | None = None


MAX_WORKBOOK_BYTES = 2 * 1024 * 1024


def _percentage(value: Decimal) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Enter a valid percentage.") from exc
    if result <= 0 or result > 50:
        raise HTTPException(
            status_code=400, detail="Percentage must be greater than 0 and at most 50."
        )
    return result


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.database_path)
    kite_gateway = KiteGateway(settings.api_key, settings.api_secret)
    upstox_gateway = UpstoxGateway(
        settings.upstox_api_key,
        settings.upstox_api_secret,
        settings.upstox_redirect_url,
    )
    dhan_gateway = DhanGateway(
        settings.dhan_client_id,
        settings.dhan_api_key,
        settings.dhan_api_secret,
        settings.dhan_access_token,
    )
    gateways: dict[str, Any] = {
        "zerodha": kite_gateway,
        "upstox": upstox_gateway,
        "dhan": dhan_gateway,
    }
    monitors: dict[str, Any] = {
        "zerodha": LiveMonitor(
            api_key=settings.api_key,
            gateway=kite_gateway,
            store=store,
            finalization_delay_seconds=settings.candle_finalization_delay_seconds,
        ),
        "upstox": UpstoxMonitor(
            gateway=upstox_gateway,
            store=store,
            finalization_delay_seconds=settings.candle_finalization_delay_seconds,
        ),
        "dhan": DhanMonitor(
            gateway=dhan_gateway,
            store=store,
            finalization_delay_seconds=settings.candle_finalization_delay_seconds,
        ),
    }
    nifty50 = Nifty50Service()
    market_movement = MarketMovementService()

    def provider_objects(provider: str) -> tuple[Any, Any]:
        key = provider.strip().lower()
        if key not in gateways:
            raise HTTPException(
                status_code=400, detail="Provider must be zerodha, upstox, or dhan."
            )
        return gateways[key], monitors[key]

    def subscribe_active_rules() -> None:
        today = datetime.now(IST).date()
        for provider, monitor in monitors.items():
            tokens = [
                rule["instrument_token"]
                for rule in store.active_rules(today, provider=provider)
            ]
            monitor.subscribe(tokens)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for monitor in monitors.values():
            monitor.stop()

    app = FastAPI(
        title="Trade M", version="0.7.0", docs_url="/api/docs", lifespan=lifespan
    )
    app.state.settings = settings
    app.state.store = store
    app.state.gateways = gateways
    app.state.monitors = monitors
    app.state.nifty50 = nifty50
    app.state.market_movement = market_movement

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/auth/login", include_in_schema=False)
    def kite_login() -> RedirectResponse:
        if not settings.kite_configured:
            return RedirectResponse(
                "/?error=" + quote("Add your Kite API key and secret to .env first.")
            )
        try:
            return RedirectResponse(kite_gateway.login_url())
        except KiteUnavailable as exc:
            return RedirectResponse("/?error=" + quote(str(exc)))

    @app.get("/auth/callback", include_in_schema=False)
    def kite_auth_callback(
        request_token: str | None = None,
        status: str | None = None,
        action: str | None = None,
    ) -> RedirectResponse:
        if status == "cancelled" or action == "logout":
            return RedirectResponse("/?error=" + quote("Zerodha login was cancelled."))
        if not request_token:
            return RedirectResponse(
                "/?error=" + quote("Zerodha did not return a request token.")
            )
        try:
            kite_gateway.authenticate(request_token)
            monitors["zerodha"].start(kite_gateway.access_token or "")
        except Exception as exc:
            return RedirectResponse("/?error=" + quote(f"Zerodha login failed: {exc}"))
        return RedirectResponse("/?login=zerodha")

    @app.get("/auth/upstox/login", include_in_schema=False)
    def upstox_login() -> RedirectResponse:
        if not settings.upstox_configured:
            return RedirectResponse(
                "/?error=" + quote("Add your Upstox API key and secret to .env first.")
            )
        return RedirectResponse(upstox_gateway.login_url())

    @app.get("/auth/upstox/callback", include_in_schema=False)
    def upstox_auth_callback(code: str | None = None) -> RedirectResponse:
        if not code:
            return RedirectResponse("/?error=" + quote("Upstox did not return a login code."))
        try:
            upstox_gateway.authenticate(code)
            monitors["upstox"].start(upstox_gateway.access_token or "")
        except Exception as exc:
            return RedirectResponse("/?error=" + quote(f"Upstox login failed: {exc}"))
        return RedirectResponse("/?login=upstox")

    @app.get("/auth/dhan/login", include_in_schema=False)
    def dhan_login() -> RedirectResponse:
        if not settings.dhan_configured:
            return RedirectResponse(
                "/?error="
                + quote(
                    "Add DHAN_CLIENT_ID and either DHAN_ACCESS_TOKEN or Dhan app "
                    "credentials to .env first."
                )
            )
        try:
            login_url = dhan_gateway.login_url()
            if dhan_gateway.authenticated:
                monitors["dhan"].start(dhan_gateway.access_token or "")
            return RedirectResponse(login_url)
        except Exception as exc:
            return RedirectResponse("/?error=" + quote(f"Dhan login failed: {exc}"))

    @app.get("/auth/dhan/callback", include_in_schema=False)
    def dhan_auth_callback(tokenId: str | None = None) -> RedirectResponse:  # noqa: N803
        if not tokenId:
            return RedirectResponse(
                "/?error=" + quote("Dhan did not return a consent token ID.")
            )
        try:
            dhan_gateway.authenticate(tokenId)
            monitors["dhan"].start(dhan_gateway.access_token or "")
        except Exception as exc:
            return RedirectResponse("/?error=" + quote(f"Dhan login failed: {exc}"))
        return RedirectResponse("/?login=dhan")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        now = datetime.now(IST)
        market_open = (
            now.weekday() < 5
            and SESSION_OPEN <= now.time().replace(tzinfo=None) < SESSION_CLOSE
        )
        providers = {
            "zerodha": {
                "configured": settings.kite_configured,
                "authenticated": kite_gateway.authenticated,
                "user_name": kite_gateway.user_name,
                "monitor": monitors["zerodha"].status(),
            },
            "upstox": {
                "configured": settings.upstox_configured,
                "authenticated": upstox_gateway.authenticated,
                "user_name": upstox_gateway.user_name,
                "monitor": monitors["upstox"].status(),
            },
            "dhan": {
                "configured": settings.dhan_configured,
                "authenticated": dhan_gateway.authenticated,
                "user_name": dhan_gateway.user_name,
                "monitor": monitors["dhan"].status(),
            },
        }
        return {
            "kite_configured": settings.kite_configured,
            "authenticated": any(item["authenticated"] for item in providers.values()),
            "user_name": kite_gateway.user_name,
            "market_open": market_open,
            "server_time": now.isoformat(),
            "monitor": providers["zerodha"]["monitor"],
            "providers": providers,
        }

    @app.get("/api/instruments/search")
    def search_instruments(
        q: str = Query(min_length=1, max_length=40),
        exchange: str = Query(default="NSE", pattern="^(NSE|BSE)$"),
        provider: str = Query(default="zerodha", pattern="^(zerodha|upstox|dhan)$"),
    ) -> list[dict[str, Any]]:
        gateway, _ = provider_objects(provider)
        try:
            return gateway.search_instruments(q, exchange)
        except KiteUnavailable as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"{provider.title()} instrument lookup failed: {exc}"
            ) from exc

    @app.get("/api/market-movement")
    def daily_market_movement() -> dict[str, Any]:
        now = datetime.now(IST)
        return market_movement.get(now.date(), gateways, now=now)

    @app.get("/api/nifty50")
    def nifty_constituents(refresh: bool = False) -> dict[str, Any]:
        result = nifty50.get(force_refresh=refresh)
        return {"symbols": result.symbols, "count": len(result.symbols), "source": result.source}

    @app.get("/api/rules")
    def list_rules() -> list[dict[str, Any]]:
        return store.daily_rules(datetime.now(IST).date())

    @app.post("/api/rules", status_code=201)
    def add_rule(payload: RuleInput) -> dict[str, Any]:
        gateway, monitor = provider_objects(payload.provider)
        exchange = payload.exchange.strip().upper()
        symbol = payload.tradingsymbol.strip().upper()
        if exchange not in {"NSE", "BSE"}:
            raise HTTPException(status_code=400, detail="Exchange must be NSE or BSE.")
        if not symbol:
            raise HTTPException(status_code=400, detail="Trading symbol is required.")
        percentage = _percentage(payload.percentage)
        try:
            rule = create_daily_rule(
                gateway,
                store,
                exchange=exchange,
                tradingsymbol=symbol,
                percentage=percentage,
                trading_date=datetime.now(IST).date(),
            )
            monitor.subscribe([rule["instrument_token"]])
            return rule
        except KiteUnavailable as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Could not create the rule: {exc}"
            ) from exc

    @app.post("/api/rules/nifty50", status_code=201)
    def add_nifty_rules(payload: NiftyBulkInput) -> dict[str, Any]:
        gateway, monitor = provider_objects(payload.provider)
        common = _percentage(payload.common_percentage)
        overrides = {
            symbol.strip().upper(): _percentage(value)
            for symbol, value in payload.percentages.items()
        }
        constituent_result = nifty50.get()
        created: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        today = datetime.now(IST).date()
        for symbol in constituent_result.symbols:
            try:
                created.append(
                    create_daily_rule(
                        gateway,
                        store,
                        exchange="NSE",
                        tradingsymbol=symbol,
                        percentage=overrides.get(symbol, common),
                        trading_date=today,
                    )
                )
            except Exception as exc:
                failures.append({"symbol": symbol, "error": str(exc)})
        monitor.subscribe([rule["instrument_token"] for rule in created])
        return {
            "created": len(created),
            "failed": len(failures),
            "failures": failures,
            "source": constituent_result.source,
            "rules": created,
        }

    @app.post("/api/rules/import", status_code=201)
    def import_rules(
        provider: str = Form(...), workbook: UploadFile = File(...)
    ) -> dict[str, Any]:
        gateway, monitor = provider_objects(provider)
        if not gateway.authenticated:
            raise HTTPException(
                status_code=401,
                detail=f"Sign in to {provider.title()} before importing stocks.",
            )
        filename = workbook.filename or ""
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Upload an .xlsx Excel workbook.")
        content = workbook.file.read(MAX_WORKBOOK_BYTES + 1)
        if len(content) > MAX_WORKBOOK_BYTES:
            raise HTTPException(
                status_code=413, detail="The Excel workbook must be 2 MB or smaller."
            )
        try:
            parsed = parse_stock_workbook(content)
        except StockWorkbookError as exc:
            detail = " ".join(exc.errors[:12])
            if len(exc.errors) > 12:
                detail += f" {len(exc.errors) - 12} more row errors were found."
            raise HTTPException(status_code=400, detail=detail) from exc

        created: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        today = datetime.now(IST).date()
        for item in parsed.stocks:
            try:
                created.append(
                    create_daily_rule(
                        gateway,
                        store,
                        exchange=item.exchange,
                        tradingsymbol=item.tradingsymbol,
                        percentage=item.percentage,
                        trading_date=today,
                    )
                )
            except Exception as exc:
                failures.append(
                    {
                        "row": item.row_number,
                        "exchange": item.exchange,
                        "symbol": item.tradingsymbol,
                        "error": str(exc),
                    }
                )
        monitor.subscribe([rule["instrument_token"] for rule in created])
        return {
            "created": len(created),
            "failed": len(failures),
            "skipped": parsed.skipped,
            "failures": failures,
            "rules": created,
        }

    @app.patch("/api/rules/{rule_id}")
    def update_rule(rule_id: int, payload: RulePatch) -> dict[str, Any]:
        if payload.percentage is None and payload.active is None:
            raise HTTPException(status_code=400, detail="No rule change was provided.")
        if payload.percentage is not None:
            rule = store.update_rule_percentage(rule_id, _percentage(payload.percentage))
            if rule is None:
                raise HTTPException(status_code=404, detail="Rule not found.")
        if payload.active is not None and not store.set_rule_active(rule_id, payload.active):
            raise HTTPException(status_code=404, detail="Rule not found.")
        rule = store.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found.")
        if rule["active"]:
            monitors[rule["provider"]].subscribe([rule["instrument_token"]])
        return rule

    @app.post("/api/rules/bulk-status")
    def bulk_rule_status(payload: BulkStatusInput) -> dict[str, Any]:
        affected = store.set_rules_active(
            datetime.now(IST).date(), payload.active, payload.rule_ids
        )
        if payload.active:
            subscribe_active_rules()
        return {"affected": affected, "active": payload.active}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int) -> dict[str, bool]:
        if not store.deactivate_rule(rule_id):
            raise HTTPException(status_code=404, detail="Rule not found.")
        return {"deleted": True}

    @app.get("/api/events")
    def events(
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return store.events_after(after_id, limit, datetime.now(IST).date())

    @app.get("/api/events/snapshot")
    def event_snapshot() -> dict[str, Any]:
        return {
            "events": store.events_after(0, 500, datetime.now(IST).date()),
            "latest_id": store.latest_event_id(),
            "server_time": datetime.now(IST).isoformat(),
        }

    @app.get("/api/events/stream")
    async def event_stream(
        request: Request,
        after_id: int = Query(default=0, ge=0),
        last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        cursor = max(after_id, last_event_id or 0)

        async def generate():
            nonlocal cursor
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            yield "retry: 2000\nevent: ready\ndata: {}\n\n"
            while not await request.is_disconnected():
                current = store.events_after(
                    cursor, 100, datetime.now(IST).date()
                )
                if current:
                    for event in current:
                        cursor = max(cursor, int(event["id"]))
                        payload = json.dumps(event, separators=(",", ":"))
                        yield f"id: {event['id']}\nevent: alert\ndata: {payload}\n\n"
                    last_heartbeat = loop.time()
                    continue
                if loop.time() - last_heartbeat >= 15:
                    yield ": keepalive\n\n"
                    last_heartbeat = loop.time()
                await asyncio.sleep(0.75)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = build_app()
