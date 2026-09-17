from __future__ import annotations

import logging
import threading
import time as time_module
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from .domain import (
    IST,
    THREE_MINUTES,
    Candle,
    CandleAggregator,
    as_decimal,
    bucket_start,
    calculate_levels,
    ensure_ist,
    market_session_open,
)
from .monitoring import ReliableMonitorMixin
from .storage import Store

logger = logging.getLogger(__name__)


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
    provider = "zerodha"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite: Any | None = None
        self.access_token: str | None = None
        self.user_name: str | None = None
        self._instruments: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._last_historical_request = 0.0

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
        from_date = datetime.combine(trading_date - timedelta(days=14), datetime.min.time())
        to_date = datetime.combine(trading_date - timedelta(days=1), datetime.max.time())
        candles = self.historical_candles(instrument_token, from_date, to_date)
        eligible = [candle for candle in candles if candle.start.date() < trading_date]
        if not eligible:
            raise KiteUnavailable(
                "No prior three-minute candle was returned. Check the symbol and "
                "historical-data subscription."
            )
        final_candle = max(eligible, key=lambda value: value.start)
        return final_candle.start.date(), final_candle.close

    def india_vix_previous_close(self, trading_date: date) -> tuple[date, Decimal]:
        vix = next(
            (
                item
                for item in self.instruments("NSE")
                if str(item.get("tradingsymbol", "")).replace(" ", "").upper()
                == "INDIAVIX"
            ),
            None,
        )
        if vix is None:
            raise KiteUnavailable("India VIX was not found in Zerodha's NSE instruments.")
        return self.previous_session_close(int(vix["instrument_token"]), trading_date)

    def completed_candles(
        self, instrument_token: int, since: datetime, until: datetime
    ) -> list[Candle]:
        """Return completed three-minute candles for reconnect recovery."""
        since = ensure_ist(since)
        until = ensure_ist(until)
        query_since = bucket_start(since) or since
        return self.historical_candles(instrument_token, query_since, until)

    def historical_candles(
        self, instrument_token: int, since: datetime, until: datetime
    ) -> list[Candle]:
        """Return archived completed three-minute candles for stop calibration."""
        client = self.require_client()
        since = ensure_ist(since)
        until = ensure_ist(until)
        with self._lock:
            wait = 0.36 - (time_module.monotonic() - self._last_historical_request)
            if wait > 0:
                time_module.sleep(wait)
            self._last_historical_request = time_module.monotonic()
        rows = client.historical_data(
            instrument_token, since, until, "3minute", continuous=False, oi=False
        )
        result: list[Candle] = []
        for row in rows:
            raw_timestamp = row["date"]
            start = ensure_ist(
                datetime.fromisoformat(raw_timestamp)
                if isinstance(raw_timestamp, str)
                else raw_timestamp
            )
            end = start + THREE_MINUTES
            if end <= until and end > since:
                result.append(
                    Candle.from_ohlc(
                        instrument_token=instrument_token,
                        start=start,
                        end=end,
                        open=as_decimal(row["open"]),
                        high=as_decimal(row["high"]),
                        low=as_decimal(row["low"]),
                        close=as_decimal(row["close"]),
                    )
                )
        return result


class LiveMonitor(ReliableMonitorMixin):
    def __init__(
        self,
        *,
        api_key: str,
        store: Store,
        finalization_delay_seconds: int,
        gateway: KiteGateway | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider = "zerodha"
        self.api_key = api_key
        self.gateway = gateway
        self.store = store
        self.finalization_delay_seconds = finalization_delay_seconds
        self.on_event = on_event or (lambda event: None)
        self.aggregator = CandleAggregator()
        self.ticker: Any | None = None
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
        self._generation = 0
        self._init_reliability()

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
            self.aggregator = CandleAggregator()
            self.last_tick_at = None
            self.connected_at = None
            self._generation += 1
            generation = self._generation
            self._finalizer_thread = threading.Thread(
                target=self._finalizer_loop,
                args=(generation,),
                name="trade-m-zerodha-candle-finalizer",
                daemon=True,
            )
            self._finalizer_thread.start()
            self._start_reliability_workers(generation)
        try:
            ticker.connect(threaded=True)
        except Exception as exc:
            self._record_error(f"Could not start Zerodha WebSocket: {exc}")
            with self._lock:
                self.running = False
            raise

    def stop(self) -> None:
        with self._lock:
            self.running = False
            self._generation += 1
            ticker = self.ticker
            self.ticker = None
            self.connected = False
            self._wake_reliability_workers()
        if ticker is not None:
            try:
                ticker.close()
            except Exception:
                pass

    def subscribe(self, tokens: list[int]) -> None:
        self._request_subscriptions(tokens)

    def _coerce_token(self, token: int | str) -> int:
        return int(token)

    def _subscribe_batches(self, tokens: set[Any]) -> set[Any]:
        unique_tokens = sorted(int(token) for token in tokens)
        with self._lock:
            ticker = self.ticker
            connected = self.connected
        if ticker is None or not connected:
            return set()
        subscribed: set[Any] = set()
        for start in range(0, len(unique_tokens), 500):
            batch = unique_tokens[start : start + 500]
            try:
                ticker.subscribe(batch)
                ticker.set_mode(ticker.MODE_FULL, batch)
                subscribed.update(batch)
            except Exception as exc:
                self._record_error(f"Zerodha subscription failed: {exc}")
        return subscribed

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
            status = {
                "running": self.running,
                "connected": self.connected,
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
                "last_tick_age_seconds": round(age, 1) if age is not None else None,
                "stale": stale,
                "processed_candles": self.processed_candles,
                "error_count": self.error_count,
                "last_prices": {str(key): str(value) for key, value in self.last_prices.items()},
            }
        status.update(self._reliability_status())
        return status

    def _on_connect(self, ws: Any, response: Any) -> None:
        with self._lock:
            if ws is not self.ticker:
                return
            self.connected = True
            self.connected_at = datetime.now(IST)
            self.last_error = None
        self._connection_ready()

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        now = datetime.now(IST)
        completed: list[Candle] = []
        with self._lock:
            if ws is not self.ticker:
                return
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
                    self._record_error(f"Ignored invalid Zerodha tick: {exc}")
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
                self._record_error(f"Zerodha candle finalizer recovered from: {exc}")
            time_module.sleep(0.5)

    def _process(self, candles: list[Candle]) -> None:
        self._process_reliably(candles)

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.error_count += 1
            self.last_error = message
        logger.warning(message)

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        with self._lock:
            if ws is not self.ticker:
                return
            self.connected = False
            self.disconnected_at = self.disconnected_at or datetime.now(IST)
            if self.running:
                self.last_error = f"WebSocket closed ({code}): {reason}"
        self._connection_lost()

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        self._record_error(f"Zerodha WebSocket error ({code}): {reason}")

    def _on_reconnect(self, ws: Any, attempts_count: int) -> None:
        with self._lock:
            self.last_error = f"Reconnecting to Zerodha (attempt {attempts_count})"

    def _on_noreconnect(self, ws: Any) -> None:
        with self._lock:
            self.connected = False
            self.last_error = "Zerodha WebSocket reconnect limit reached. Restart the app."
        self._connection_lost()


def create_daily_rule(
    gateway: Any,
    store: Store,
    *,
    exchange: str,
    tradingsymbol: str,
    percentage: Decimal,
    trading_date: date,
) -> dict[str, Any]:
    instrument = gateway.resolve_instrument(exchange, tradingsymbol)
    token: int | str = instrument["instrument_token"]
    if gateway.provider == "zerodha":
        token = int(token)
    history: list[Candle] = []
    history_error: Exception | None = None
    historical_method = getattr(gateway, "historical_candles", None)
    if callable(historical_method):
        try:
            history = historical_method(
                token,
                datetime.combine(
                    trading_date - timedelta(days=28), datetime.min.time()
                ),
                datetime.combine(trading_date, datetime.min.time()),
            )
        except Exception as exc:
            history_error = exc
            logger.warning(
                "%s stop-history preload failed for %s:%s: %s",
                gateway.provider.title(),
                exchange,
                tradingsymbol,
                exc,
            )
    eligible = [candle for candle in history if candle.start.date() < trading_date]
    if eligible:
        store.save_market_candles(gateway.provider, history)
        final_candle = max(eligible, key=lambda value: value.start)
        reference_date, reference_close = final_candle.start.date(), final_candle.close
    else:
        try:
            reference_date, reference_close = gateway.previous_session_close(
                token, trading_date
            )
        except Exception:
            if history_error is not None:
                raise history_error
            raise
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
        provider=gateway.provider,
    )
    result = store.get_rule(rule_id)
    if result is None:
        raise RuntimeError("Rule was saved but could not be loaded")
    return result
