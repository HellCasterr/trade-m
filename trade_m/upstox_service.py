from __future__ import annotations

import json
import logging
import threading
import time as time_module
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .domain import (
    Candle,
    CandleAggregator,
    IST,
    THREE_MINUTES,
    as_decimal,
    bucket_start,
    ensure_ist,
    market_session_open,
)
from .kite_service import KiteUnavailable
from .storage import Store


logger = logging.getLogger(__name__)

INDIA_VIX_INSTRUMENT_KEY = "NSE_INDEX|India VIX"


def _upstox_module() -> Any:
    try:
        import upstox_client
    except ImportError as exc:
        raise KiteUnavailable(
            "upstox-python-sdk is not installed. Run setup_windows.bat again."
        ) from exc
    return upstox_client


class UpstoxGateway:
    provider = "upstox"

    def __init__(self, api_key: str, api_secret: str, redirect_url: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_url = redirect_url
        self.access_token: str | None = None
        self.user_name: str | None = None
        self.api_client: Any | None = None
        self._lock = threading.RLock()
        self._last_historical_request = 0.0

    def login_url(self) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.api_key,
                "redirect_uri": self.redirect_url,
            }
        )
        return f"https://api.upstox.com/v2/login/authorization/dialog?{query}"

    def authenticate(self, code: str) -> dict[str, Any]:
        try:
            response = requests.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "code": code,
                    "client_id": self.api_key,
                    "client_secret": self.api_secret,
                    "redirect_uri": self.redirect_url,
                    "grant_type": "authorization_code",
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise KiteUnavailable(f"Upstox token exchange failed: {exc}") from exc
        try:
            session = response.json()
        except requests.JSONDecodeError as exc:
            raise KiteUnavailable(
                f"Upstox token exchange failed ({response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
        if not response.ok:
            raise KiteUnavailable(
                f"Upstox token exchange failed ({response.status_code}): {session}"
            )

        access_token = session.get("access_token")
        if not access_token:
            raise KiteUnavailable("Upstox did not return an access token.")
        upstox = _upstox_module()
        configuration = upstox.Configuration()
        configuration.access_token = access_token
        with self._lock:
            self.access_token = str(access_token)
            self.api_client = upstox.ApiClient(configuration)
            self.user_name = str(session.get("user_name") or "Upstox user")
        return session

    @property
    def authenticated(self) -> bool:
        return self.api_client is not None and bool(self.access_token)

    def require_client(self) -> Any:
        if self.api_client is None:
            raise KiteUnavailable("Sign in to Upstox before using its market data.")
        return self.api_client

    @staticmethod
    def _normalise_instrument(item: Any, exchange: str) -> dict[str, Any]:
        if not isinstance(item, dict) and hasattr(item, "to_dict"):
            item = item.to_dict()
        if not isinstance(item, dict):
            raise KiteUnavailable("Upstox returned an unsupported instrument record.")
        return {
            "exchange": exchange,
            "tradingsymbol": str(
                item.get("trading_symbol") or item.get("tradingsymbol") or ""
            ),
            "name": str(item.get("name") or item.get("short_name") or ""),
            "instrument_token": str(
                item.get("instrument_key") or item.get("instrument_token") or ""
            ),
        }

    def search_instruments(self, query: str, exchange: str = "NSE") -> list[dict[str, Any]]:
        upstox = _upstox_module()
        response = upstox.InstrumentsApi(self.require_client()).search_instrument(
            query.strip(),
            exchanges=exchange.upper(),
            segments="EQ",
            instrument_types="EQ",
            records=20,
        )
        results: list[dict[str, Any]] = []
        for item in response.data or []:
            normalised = self._normalise_instrument(item, exchange.upper())
            if normalised["tradingsymbol"] and normalised["instrument_token"]:
                results.append(normalised)
        return results

    def resolve_instrument(self, exchange: str, tradingsymbol: str) -> dict[str, Any]:
        target = tradingsymbol.strip().upper()
        for item in self.search_instruments(target, exchange):
            if item["tradingsymbol"].upper() == target:
                return item
        raise KiteUnavailable(
            f"No exact {exchange.upper()}:{target} instrument was found on Upstox."
        )

    def previous_session_close(
        self, instrument_token: str, trading_date: date
    ) -> tuple[date, Decimal]:
        with self._lock:
            wait = 0.15 - (time_module.monotonic() - self._last_historical_request)
            if wait > 0:
                time_module.sleep(wait)
            self._last_historical_request = time_module.monotonic()
        upstox = _upstox_module()
        history = upstox.HistoryV3Api(self.require_client())
        from_date = (trading_date - timedelta(days=14)).isoformat()
        to_date = (trading_date - timedelta(days=1)).isoformat()
        response = history.get_historical_candle_data1(
            instrument_token, "minutes", "3", to_date, from_date
        )
        eligible: list[tuple[datetime, list[Any]]] = []
        for candle in response.data.candles or []:
            timestamp = ensure_ist(datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00")))
            if timestamp.date() < trading_date:
                eligible.append((timestamp, candle))
        if not eligible:
            raise KiteUnavailable(
                "No prior Upstox three-minute candle was returned for this instrument."
            )
        timestamp, final_candle = max(eligible, key=lambda value: value[0])
        return timestamp.date(), as_decimal(final_candle[4])

    def india_vix_previous_close(self, trading_date: date) -> tuple[date, Decimal]:
        return self.previous_session_close(INDIA_VIX_INSTRUMENT_KEY, trading_date)

    def completed_candles(
        self, instrument_token: str, since: datetime, until: datetime
    ) -> list[Candle]:
        since = ensure_ist(since)
        until = ensure_ist(until)
        with self._lock:
            wait = 0.15 - (time_module.monotonic() - self._last_historical_request)
            if wait > 0:
                time_module.sleep(wait)
            self._last_historical_request = time_module.monotonic()
        upstox = _upstox_module()
        response = upstox.HistoryV3Api(
            self.require_client()
        ).get_intra_day_candle_data(instrument_token, "minutes", "3")
        result: list[Candle] = []
        for row in response.data.candles or []:
            start = ensure_ist(
                datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            )
            end = start + THREE_MINUTES
            if end <= until and end > since:
                result.append(
                    Candle.from_ohlc(
                        instrument_token=instrument_token,
                        start=start,
                        end=end,
                        open=as_decimal(row[1]),
                        high=as_decimal(row[2]),
                        low=as_decimal(row[3]),
                        close=as_decimal(row[4]),
                    )
                )
        return result


class UpstoxMonitor:
    provider = "upstox"

    def __init__(
        self,
        *,
        gateway: UpstoxGateway,
        store: Store,
        finalization_delay_seconds: int,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.finalization_delay_seconds = finalization_delay_seconds
        self.on_event = on_event or (lambda event: None)
        self.aggregator = CandleAggregator()
        self.streamer: Any | None = None
        self.connected = False
        self.running = False
        self.last_error: str | None = None
        self.last_tick_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.disconnected_at: datetime | None = None
        self.processed_candles = 0
        self.error_count = 0
        self.last_prices: dict[int | str, Decimal] = {}
        self._lock = threading.RLock()
        self._finalizer_thread: threading.Thread | None = None
        self._recovery_thread: threading.Thread | None = None
        self._generation = 0

    def start(self, access_token: str) -> None:
        if not access_token:
            raise KiteUnavailable("Upstox access token is missing.")
        upstox = _upstox_module()
        with self._lock:
            if self.streamer is not None:
                try:
                    self.streamer.disconnect()
                except Exception:
                    pass
            streamer = upstox.MarketDataStreamerV3(self.gateway.require_client())
            streamer.auto_reconnect(True, 5, 50)
            streamer.on("open", self._on_open)
            streamer.on("message", self._on_message)
            streamer.on("close", self._on_close)
            streamer.on("error", self._on_error)
            streamer.on("reconnecting", self._on_reconnecting)
            streamer.on("autoReconnectStopped", self._on_reconnect_stopped)
            self.streamer = streamer
            self.running = True
            self.last_error = None
            self.aggregator = CandleAggregator()
            self.last_tick_at = None
            self.connected_at = None
            self._generation += 1
            generation = self._generation
            self._finalizer_thread = threading.Thread(
                target=self._finalizer_loop,
                args=(generation,),
                name="trade-m-upstox-candle-finalizer",
                daemon=True,
            )
            self._finalizer_thread.start()
        try:
            streamer.connect()
        except Exception as exc:
            self._record_error(f"Could not start Upstox WebSocket: {exc}")
            with self._lock:
                self.running = False
            raise

    def stop(self) -> None:
        with self._lock:
            self.running = False
            self._generation += 1
            streamer = self.streamer
            self.streamer = None
            self.connected = False
        if streamer is not None:
            try:
                streamer.disconnect()
            except Exception:
                pass

    def subscribe(self, tokens: list[int | str]) -> None:
        keys = sorted(set(str(token) for token in tokens))
        with self._lock:
            streamer = self.streamer
            connected = self.connected
        if streamer is not None and connected:
            for start in range(0, len(keys), 500):
                try:
                    streamer.subscribe(keys[start : start + 500], "ltpc")
                except Exception as exc:
                    self._record_error(f"Upstox subscription failed: {exc}")

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(IST)
            anchor = self.last_tick_at or self.connected_at
            age = (now - anchor).total_seconds() if anchor else None
            has_rules = bool(self.store.active_rules(now.date(), provider=self.provider))
            stale = bool(
                self.running
                and self.connected
                and has_rules
                and market_session_open(now)
                and (age is None or age > 45)
            )
            return {
                "running": self.running,
                "connected": self.connected,
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
                "last_tick_age_seconds": round(age, 1) if age is not None else None,
                "stale": stale,
                "recovering": bool(self._recovery_thread and self._recovery_thread.is_alive()),
                "processed_candles": self.processed_candles,
                "error_count": self.error_count,
                "last_prices": {str(key): str(value) for key, value in self.last_prices.items()},
            }

    def _on_open(self) -> None:
        with self._lock:
            self.connected = True
            self.connected_at = datetime.now(IST)
            self.last_error = None
            recovery_from = self.disconnected_at
            generation = self._generation
        keys = [
            rule["instrument_token"]
            for rule in self.store.active_rules(datetime.now(IST).date(), provider=self.provider)
        ]
        self.subscribe(keys)
        if recovery_from and keys:
            self._start_recovery(keys, recovery_from, generation)

    def _on_message(self, message: dict[str, Any]) -> None:
        now = datetime.now(IST)
        completed: list[Candle] = []
        with self._lock:
            for key, feed in (message.get("feeds") or {}).items():
                try:
                    ltpc = feed.get("ltpc") or feed.get("fullFeed", {}).get(
                        "marketFF", {}
                    ).get("ltpc")
                    if not ltpc:
                        continue
                    price = as_decimal(ltpc["ltp"])
                    raw_timestamp = ltpc.get("ltt")
                    timestamp = (
                        datetime.fromtimestamp(int(raw_timestamp) / 1000, tz=UTC).astimezone(IST)
                        if raw_timestamp
                        else now
                    )
                    self.last_prices[key] = price
                    self.last_tick_at = now
                    completed.extend(self.aggregator.add_tick(key, price, timestamp))
                except (KeyError, TypeError, ValueError) as exc:
                    self._record_error(f"Ignored invalid Upstox tick: {exc}")
        self._process(completed)

    def _finalizer_loop(self, generation: int) -> None:
        while self.running and self._generation == generation:
            try:
                with self._lock:
                    completed = self.aggregator.finalize_due(
                        datetime.now(IST), self.finalization_delay_seconds
                    )
                self._process(completed)
            except Exception as exc:
                self._record_error(f"Upstox candle finalizer recovered from: {exc}")
            time_module.sleep(0.5)

    def _process(self, candles: list[Candle]) -> None:
        for candle in candles:
            try:
                events = self.store.evaluate_candle(candle, provider=self.provider)
                with self._lock:
                    self.processed_candles += 1
                for event in events:
                    try:
                        self.on_event(event)
                    except Exception as exc:
                        self._record_error(f"Upstox event callback failed: {exc}")
            except Exception as exc:
                self._record_error(f"Upstox candle processing failed: {exc}")

    def _start_recovery(
        self, tokens: list[int | str], since: datetime, generation: int
    ) -> None:
        with self._lock:
            if self._recovery_thread and self._recovery_thread.is_alive():
                return
            self._recovery_thread = threading.Thread(
                target=self._recover,
                args=(sorted(set(str(token) for token in tokens)), since, generation),
                name="trade-m-upstox-recovery",
                daemon=True,
            )
            self._recovery_thread.start()

    def _recover(self, tokens: list[str], since: datetime, generation: int) -> None:
        failures: list[str] = []
        first_until = datetime.now(IST)
        failures.extend(self._recover_pass(tokens, since, first_until, generation))
        current_start = bucket_start(first_until)
        if current_start is not None and since < current_start + THREE_MINUTES:
            due = current_start + THREE_MINUTES + timedelta(
                seconds=self.finalization_delay_seconds
            )
            while self.running and self._generation == generation:
                remaining = (due - datetime.now(IST)).total_seconds()
                if remaining <= 0:
                    break
                time_module.sleep(min(0.5, remaining))
            if self.running and self._generation == generation:
                failures.extend(
                    self._recover_pass(
                        tokens, since, datetime.now(IST), generation
                    )
                )
        with self._lock:
            if self._generation != generation:
                return
            if failures:
                self.error_count += len(failures)
                self.last_error = (
                    f"Upstox recovery failed for {len(failures)} instrument(s): "
                    f"{failures[0]}"
                )
            else:
                self.disconnected_at = None

    def _recover_pass(
        self,
        tokens: list[str],
        since: datetime,
        until: datetime,
        generation: int,
    ) -> list[str]:
        failures: list[str] = []
        for token in tokens:
            if not self.running or self._generation != generation:
                break
            try:
                self._process(self.gateway.completed_candles(token, since, until))
            except Exception as exc:
                failures.append(f"{token}: {exc}")
        return failures

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.error_count += 1
            self.last_error = message
        logger.warning(message)

    def _on_close(self, code: int, reason: str) -> None:
        with self._lock:
            self.connected = False
            self.disconnected_at = self.disconnected_at or datetime.now(IST)
            if self.running:
                self.last_error = f"Upstox WebSocket closed ({code}): {reason}"

    def _on_error(self, error: Any) -> None:
        self._record_error(f"Upstox WebSocket error: {error}")

    def _on_reconnecting(self, message: str) -> None:
        with self._lock:
            self.last_error = str(message)

    def _on_reconnect_stopped(self, message: str) -> None:
        with self._lock:
            self.connected = False
            self.last_error = f"Upstox reconnect stopped: {message}"
