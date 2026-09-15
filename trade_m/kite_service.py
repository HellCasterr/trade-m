from __future__ import annotations

import threading
import time as time_module
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from .domain import Candle, CandleAggregator, IST, as_decimal, calculate_levels, ensure_ist
from .storage import Store


class KiteUnavailable(RuntimeError):
    pass


def _kite_classes() -> tuple[type[Any], type[Any]]:
    try:
        from kiteconnect import KiteConnect, KiteTicker
    except ImportError as exc:
        raise KiteUnavailable(
            "kiteconnect is not installed. Run setup_windows.bat first."
        ) from exc
    return KiteConnect, KiteTicker


class KiteGateway:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite: Any | None = None
        self.access_token: str | None = None
        self.user_name: str | None = None
        self._instruments: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def _new_client(self) -> Any:
        KiteConnect, _ = _kite_classes()
        return KiteConnect(api_key=self.api_key)

    def login_url(self) -> str:
        return self._new_client().login_url()

    def authenticate(self, request_token: str) -> dict[str, Any]:
        client = self._new_client()
        session = client.generate_session(request_token, api_secret=self.api_secret)
        access_token = session["access_token"]
        client.set_access_token(access_token)
        with self._lock:
            self.kite = client
            self.access_token = access_token
            self.user_name = session.get("user_name") or session.get("user_shortname")
            self._instruments.clear()
        return session

    @property
    def authenticated(self) -> bool:
        return self.kite is not None and bool(self.access_token)

    def require_client(self) -> Any:
        if self.kite is None:
            raise KiteUnavailable("Sign in to Zerodha before using live market data.")
        return self.kite

    def instruments(self, exchange: str) -> list[dict[str, Any]]:
        exchange = exchange.upper()
        with self._lock:
            cached = self._instruments.get(exchange)
        if cached is not None:
            return cached
        values = self.require_client().instruments(exchange)
        with self._lock:
            self._instruments[exchange] = values
        return values

    def search_instruments(self, query: str, exchange: str = "NSE") -> list[dict[str, Any]]:
        query = query.strip().upper()
        if not query:
            return []
        matches: list[dict[str, Any]] = []
        for item in self.instruments(exchange):
            symbol = str(item.get("tradingsymbol", ""))
            name = str(item.get("name", ""))
            instrument_type = str(item.get("instrument_type", ""))
            if instrument_type not in {"EQ", "BE", "BZ", "SM", "ST"}:
                continue
            if query in symbol.upper() or query in name.upper():
                matches.append(
                    {
                        "exchange": exchange,
                        "tradingsymbol": symbol,
                        "name": name,
                        "instrument_token": int(item["instrument_token"]),
                    }
                )
            if len(matches) >= 20:
                break
        return matches

    def resolve_instrument(self, exchange: str, tradingsymbol: str) -> dict[str, Any]:
        target = tradingsymbol.strip().upper()
        for item in self.instruments(exchange):
            if str(item.get("tradingsymbol", "")).upper() == target:
                return item
        raise KiteUnavailable(f"No exact {exchange}:{target} instrument was found.")

    def previous_session_close(
        self, instrument_token: int, trading_date: date
    ) -> tuple[date, Decimal]:
        client = self.require_client()
        from_date = datetime.combine(trading_date - timedelta(days=14), datetime.min.time())
        to_date = datetime.combine(trading_date - timedelta(days=1), datetime.max.time())
        candles = client.historical_data(
            instrument_token, from_date, to_date, "3minute", continuous=False, oi=False
        )
        eligible: list[tuple[datetime, dict[str, Any]]] = []
        for candle in candles:
            raw_timestamp = candle["date"]
            if isinstance(raw_timestamp, str):
                timestamp = datetime.fromisoformat(raw_timestamp)
            else:
                timestamp = raw_timestamp
            timestamp = ensure_ist(timestamp)
            if timestamp.date() < trading_date:
                eligible.append((timestamp, candle))
        if not eligible:
            raise KiteUnavailable(
                "No prior three-minute candle was returned. Check the symbol and historical-data subscription."
            )
        timestamp, final_candle = max(eligible, key=lambda value: value[0])
        return timestamp.date(), as_decimal(final_candle["close"])


class LiveMonitor:
    def __init__(
        self,
        *,
        api_key: str,
        store: Store,
        finalization_delay_seconds: int,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.store = store
        self.finalization_delay_seconds = finalization_delay_seconds
        self.on_event = on_event or (lambda event: None)
        self.aggregator = CandleAggregator()
        self.ticker: Any | None = None
        self.connected = False
        self.running = False
        self.last_error: str | None = None
        self.last_tick_at: datetime | None = None
        self.last_prices: dict[int, Decimal] = {}
        self._lock = threading.RLock()
        self._finalizer_thread: threading.Thread | None = None

    def start(self, access_token: str) -> None:
        _, KiteTicker = _kite_classes()
        with self._lock:
            if self.ticker is not None:
                try:
                    self.ticker.close()
                except Exception:
                    pass
            ticker = KiteTicker(
                self.api_key,
                access_token,
                reconnect=True,
                reconnect_max_tries=50,
                reconnect_max_delay=60,
            )
            ticker.on_ticks = self._on_ticks
            ticker.on_connect = self._on_connect
            ticker.on_close = self._on_close
            ticker.on_error = self._on_error
            ticker.on_reconnect = self._on_reconnect
            ticker.on_noreconnect = self._on_noreconnect
            self.ticker = ticker
            self.running = True
            self.last_error = None
            if not self._finalizer_thread or not self._finalizer_thread.is_alive():
                self._finalizer_thread = threading.Thread(
                    target=self._finalizer_loop,
                    name="trade-m-candle-finalizer",
                    daemon=True,
                )
                self._finalizer_thread.start()
        ticker.connect(threaded=True)

    def stop(self) -> None:
        with self._lock:
            self.running = False
            ticker = self.ticker
            self.ticker = None
            self.connected = False
        if ticker is not None:
            try:
                ticker.close()
            except Exception:
                pass

    def subscribe(self, tokens: list[int]) -> None:
        unique_tokens = sorted(set(int(token) for token in tokens))
        with self._lock:
            ticker = self.ticker
            connected = self.connected
        if ticker is not None and connected and unique_tokens:
            ticker.subscribe(unique_tokens)
            ticker.set_mode(ticker.MODE_FULL, unique_tokens)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "connected": self.connected,
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
                "last_prices": {str(key): str(value) for key, value in self.last_prices.items()},
            }

    def _on_connect(self, ws: Any, response: Any) -> None:
        with self._lock:
            self.connected = True
            self.last_error = None
        tokens = [
            int(rule["instrument_token"])
            for rule in self.store.active_rules(datetime.now(IST).date())
        ]
        if tokens:
            ws.subscribe(sorted(set(tokens)))
            ws.set_mode(ws.MODE_FULL, sorted(set(tokens)))

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        now = datetime.now(IST)
        completed: list[Candle] = []
        with self._lock:
            self.last_tick_at = now
            for tick in ticks:
                try:
                    token = int(tick["instrument_token"])
                    price = as_decimal(tick["last_price"])
                    timestamp = (
                        tick.get("exchange_timestamp")
                        or tick.get("last_trade_time")
                        or now
                    )
                    timestamp = ensure_ist(timestamp)
                    self.last_prices[token] = price
                    completed.extend(self.aggregator.add_tick(token, price, timestamp))
                except (KeyError, TypeError, ValueError) as exc:
                    self.last_error = f"Ignored invalid tick: {exc}"
        self._process(completed)

    def _finalizer_loop(self) -> None:
        while self.running:
            with self._lock:
                completed = self.aggregator.finalize_due(
                    datetime.now(IST), self.finalization_delay_seconds
                )
            self._process(completed)
            time_module.sleep(0.5)

    def _process(self, candles: list[Candle]) -> None:
        for candle in candles:
            for event in self.store.evaluate_candle(candle):
                self.on_event(event)

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        with self._lock:
            self.connected = False
            if self.running:
                self.last_error = f"WebSocket closed ({code}): {reason}"

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        with self._lock:
            self.last_error = f"WebSocket error ({code}): {reason}"

    def _on_reconnect(self, ws: Any, attempts_count: int) -> None:
        with self._lock:
            self.last_error = f"Reconnecting to Zerodha (attempt {attempts_count})"

    def _on_noreconnect(self, ws: Any) -> None:
        with self._lock:
            self.connected = False
            self.last_error = "Zerodha WebSocket reconnect limit reached. Restart the app."


def create_daily_rule(
    gateway: KiteGateway,
    store: Store,
    *,
    exchange: str,
    tradingsymbol: str,
    percentage: Decimal,
    trading_date: date,
) -> dict[str, Any]:
    instrument = gateway.resolve_instrument(exchange, tradingsymbol)
    token = int(instrument["instrument_token"])
    reference_date, reference_close = gateway.previous_session_close(token, trading_date)
    upper, lower = calculate_levels(reference_close, percentage)
    rule_id = store.upsert_rule(
        exchange=exchange,
        tradingsymbol=str(instrument["tradingsymbol"]),
        instrument_token=token,
        trading_date=trading_date,
        percentage=percentage,
        reference_date=reference_date,
        reference_close=reference_close,
        upper_level=upper,
        lower_level=lower,
    )
    result = store.get_rule(rule_id)
    if result is None:
        raise RuntimeError("Rule was saved but could not be loaded")
    return result
