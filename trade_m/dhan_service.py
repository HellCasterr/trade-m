from __future__ import annotations

import csv
import io
import json
import logging
import struct
import threading
import time as time_module
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .domain import (
    IST,
    THREE_MINUTES,
    Candle,
    CandleAggregator,
    as_decimal,
    bucket_start,
    ensure_ist,
    market_session_open,
)
from .kite_service import KiteUnavailable
from .monitoring import ReliableMonitorMixin
from .storage import Store

logger = logging.getLogger(__name__)


INSTRUMENTS_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
API_URL = "https://api.dhan.co/v2"
AUTH_URL = "https://auth.dhan.co/app"
SEGMENT_CODES = {"NSE_EQ": 1, "BSE_EQ": 4}
CODE_SEGMENTS = {value: key for key, value in SEGMENT_CODES.items()}


def encode_instrument(exchange_segment: str, security_id: str | int) -> str:
    segment = exchange_segment.strip().upper()
    if segment not in SEGMENT_CODES:
        raise ValueError(f"Unsupported Dhan exchange segment: {exchange_segment}")
    return f"{segment}|{str(security_id).strip()}"


def decode_instrument(instrument_token: str | int) -> tuple[str, str]:
    parts = str(instrument_token).split("|", 1)
    if len(parts) != 2 or parts[0] not in SEGMENT_CODES or not parts[1]:
        raise ValueError(f"Invalid Dhan instrument token: {instrument_token}")
    return parts[0], parts[1]


def parse_ticker_packet(data: bytes) -> tuple[str, Decimal, datetime] | None:
    """Return token, LTP and exchange timestamp from a Dhan ticker packet."""
    if len(data) < 16 or data[0] != 2:
        return None
    _, message_length, exchange_code, security_id, price, epoch = struct.unpack(
        "<BHBIfI", data[:16]
    )
    if message_length < 16 or exchange_code not in CODE_SEGMENTS:
        return None
    token = encode_instrument(CODE_SEGMENTS[exchange_code], security_id)
    timestamp = datetime.fromtimestamp(epoch, tz=UTC).astimezone(IST)
    return token, as_decimal(f"{price:.2f}"), timestamp


def previous_close_from_intraday(
    payload: dict[str, Any], trading_date: date
) -> tuple[date, Decimal]:
    """Aggregate Dhan one-minute rows into aligned three-minute groups."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    timestamps = data.get("timestamp") or []
    closes = data.get("close") or []
    if len(timestamps) != len(closes):
        raise KiteUnavailable("Dhan returned incomplete historical close data.")
    groups: dict[tuple[date, datetime], list[tuple[datetime, Decimal]]] = {}
    for raw_timestamp, raw_close in zip(timestamps, closes):
        timestamp = ensure_ist(datetime.fromtimestamp(int(raw_timestamp), tz=UTC))
        start = bucket_start(timestamp)
        if start is None or timestamp.date() >= trading_date:
            continue
        groups.setdefault((timestamp.date(), start), []).append(
            (timestamp, as_decimal(raw_close))
        )
    if not groups:
        raise KiteUnavailable(
            "No prior Dhan one-minute candle was returned for this instrument."
        )
    (reference_date, _), candles = max(groups.items(), key=lambda item: item[0])
    _, final_close = max(candles, key=lambda item: item[0])
    return reference_date, final_close


def completed_candles_from_intraday(
    payload: dict[str, Any], instrument_token: str, since: datetime, until: datetime
) -> list[Candle]:
    """Aggregate Dhan one-minute history into completed three-minute candles."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    columns = {
        name: data.get(name) or [] for name in ("timestamp", "open", "high", "low", "close")
    }
    lengths = {len(values) for values in columns.values()}
    if len(lengths) > 1:
        raise KiteUnavailable("Dhan returned incomplete historical OHLC data.")
    groups: dict[datetime, list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]]] = {}
    for values in zip(
        columns["timestamp"],
        columns["open"],
        columns["high"],
        columns["low"],
        columns["close"],
    ):
        raw_timestamp, raw_open, raw_high, raw_low, raw_close = values
        timestamp = ensure_ist(datetime.fromtimestamp(int(raw_timestamp), tz=UTC))
        start = bucket_start(timestamp)
        if start is None:
            continue
        end = start + THREE_MINUTES
        if end <= until and end > since:
            groups.setdefault(start, []).append(
                (
                    timestamp,
                    as_decimal(raw_open),
                    as_decimal(raw_high),
                    as_decimal(raw_low),
                    as_decimal(raw_close),
                )
            )
    result: list[Candle] = []
    for start, rows in sorted(groups.items()):
        rows.sort(key=lambda row: row[0])
        result.append(
            Candle.from_ohlc(
                instrument_token=instrument_token,
                start=start,
                end=start + THREE_MINUTES,
                open=rows[0][1],
                high=max(row[2] for row in rows),
                low=min(row[3] for row in rows),
                close=rows[-1][4],
            )
        )
    return result


class DhanGateway:
    provider = "dhan"

    def __init__(
        self,
        client_id: str,
        api_key: str,
        api_secret: str,
        access_token: str = "",
    ) -> None:
        self.client_id = client_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.initial_access_token = access_token
        self.access_token: str | None = None
        self.user_name: str | None = None
        self._instruments: list[dict[str, Any]] | None = None
        self._india_vix_security_id: str | None = None
        self._lock = threading.RLock()
        self._last_historical_request = 0.0

    @staticmethod
    def _request_json(request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise KiteUnavailable(f"DhanHQ request failed ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KiteUnavailable(f"DhanHQ request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise KiteUnavailable("DhanHQ returned an unsupported response.")
        return result

    def login_url(self) -> str:
        if self.initial_access_token:
            self.authenticate_access_token(self.initial_access_token)
            return "/?login=dhan"
        if not (self.client_id and self.api_key and self.api_secret):
            raise KiteUnavailable(
                "Add DHAN_CLIENT_ID plus either DHAN_ACCESS_TOKEN or Dhan app credentials."
            )
        request = Request(
            f"{AUTH_URL}/generate-consent?{urlencode({'client_id': self.client_id})}",
            data=b"",
            method="POST",
            headers={"app_id": self.api_key, "app_secret": self.api_secret},
        )
        response = self._request_json(request)
        consent_id = response.get("consentAppId") or response.get("consent_app_id")
        if not consent_id:
            raise KiteUnavailable("DhanHQ did not return a consent session ID.")
        return (
            "https://auth.dhan.co/login/consentApp-login?consentAppId="
            + quote(str(consent_id), safe="")
        )

    def authenticate(self, token_id: str) -> dict[str, Any]:
        request = Request(
            f"{AUTH_URL}/consumeApp-consent?{urlencode({'tokenId': token_id})}",
            method="GET",
            headers={"app_id": self.api_key, "app_secret": self.api_secret},
        )
        session = self._request_json(request)
        access_token = session.get("accessToken") or session.get("access_token")
        returned_client_id = session.get("dhanClientId") or session.get("client_id")
        if not access_token:
            raise KiteUnavailable("DhanHQ did not return an access token.")
        if returned_client_id and str(returned_client_id) != self.client_id:
            raise KiteUnavailable("DhanHQ returned a different client ID than configured.")
        self._set_session(
            str(access_token),
            str(session.get("dhanClientName") or session.get("client_name") or "Dhan user"),
        )
        return session

    def authenticate_access_token(self, access_token: str) -> dict[str, Any]:
        if not self.client_id:
            raise KiteUnavailable("DHAN_CLIENT_ID is required with a Dhan access token.")
        request = Request(
            f"{API_URL}/profile",
            method="GET",
            headers={"access-token": access_token, "Accept": "application/json"},
        )
        profile = self._request_json(request)
        returned_client_id = profile.get("dhanClientId") or profile.get("clientId")
        if returned_client_id and str(returned_client_id) != self.client_id:
            raise KiteUnavailable("The Dhan access token belongs to a different client ID.")
        self._set_session(
            access_token,
            str(profile.get("dhanClientName") or profile.get("clientName") or "Dhan user"),
        )
        return profile

    def _set_session(self, access_token: str, user_name: str) -> None:
        with self._lock:
            self.access_token = access_token
            self.user_name = user_name

    @property
    def authenticated(self) -> bool:
        return bool(self.client_id and self.access_token)

    def require_access_token(self) -> str:
        if not self.access_token:
            raise KiteUnavailable("Sign in to Dhan before using its market data.")
        return self.access_token

    @staticmethod
    def _normalise_instrument(row: dict[str, Any]) -> dict[str, Any] | None:
        exchange = str(row.get("SEM_EXM_EXCH_ID") or row.get("EXCH_ID") or "").upper()
        segment = str(row.get("SEM_SEGMENT") or row.get("SEGMENT") or "").upper()
        instrument = str(
            row.get("SEM_INSTRUMENT_NAME") or row.get("INSTRUMENT") or ""
        ).upper()
        series = str(row.get("SEM_SERIES") or row.get("SERIES") or "").upper()
        if exchange not in {"NSE", "BSE"}:
            return None
        if segment and segment != "E":
            return None
        if instrument and instrument not in {"EQUITY", "EQ"}:
            return None
        if series and series not in {"EQ", "A"}:
            return None
        symbol = str(
            row.get("SEM_TRADING_SYMBOL")
            or row.get("SYMBOL_NAME")
            or row.get("SM_SYMBOL_NAME")
            or ""
        ).strip()
        security_id = str(
            row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID") or ""
        ).strip()
        if not symbol or not security_id:
            return None
        return {
            "exchange": exchange,
            "tradingsymbol": symbol,
            "name": str(
                row.get("SEM_CUSTOM_SYMBOL")
                or row.get("DISPLAY_NAME")
                or row.get("SM_SYMBOL_NAME")
                or symbol
            ).strip(),
            "instrument_token": encode_instrument(f"{exchange}_EQ", security_id),
        }

    def _load_instruments(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._instruments is not None:
                return self._instruments
        try:
            with urlopen(INSTRUMENTS_URL, timeout=45) as response:
                text = response.read().decode("utf-8-sig")
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
            raise KiteUnavailable(f"Could not download Dhan's instrument list: {exc}") from exc
        rows: list[dict[str, Any]] = []
        for raw in csv.DictReader(io.StringIO(text)):
            item = self._normalise_instrument(raw)
            if item is not None:
                rows.append(item)
        if not rows:
            raise KiteUnavailable("Dhan's instrument list contained no NSE/BSE equities.")
        with self._lock:
            self._instruments = rows
        return rows

    @staticmethod
    def _index_security_id(row: dict[str, Any], target: str) -> str | None:
        values = (
            row.get("SEM_TRADING_SYMBOL"),
            row.get("SYMBOL_NAME"),
            row.get("SM_SYMBOL_NAME"),
            row.get("SEM_CUSTOM_SYMBOL"),
            row.get("DISPLAY_NAME"),
        )
        names = {
            str(value or "").replace(" ", "").replace("_", "").upper()
            for value in values
        }
        if target.replace(" ", "").upper() not in names:
            return None
        security_id = str(
            row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID") or ""
        ).strip()
        return security_id or None

    def _find_india_vix_security_id(self) -> str:
        with self._lock:
            if self._india_vix_security_id is not None:
                return self._india_vix_security_id
        try:
            with urlopen(INSTRUMENTS_URL, timeout=45) as response:
                text = response.read().decode("utf-8-sig")
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
            raise KiteUnavailable(f"Could not download Dhan's instrument list: {exc}") from exc
        for row in csv.DictReader(io.StringIO(text)):
            security_id = self._index_security_id(row, "INDIA VIX")
            if security_id:
                with self._lock:
                    self._india_vix_security_id = security_id
                return security_id
        raise KiteUnavailable("India VIX was not found in Dhan's instrument list.")

    def search_instruments(self, query: str, exchange: str = "NSE") -> list[dict[str, Any]]:
        self.require_access_token()
        target = query.strip().upper()
        market = exchange.strip().upper()
        matches = [
            item
            for item in self._load_instruments()
            if item["exchange"] == market
            and (target in item["tradingsymbol"].upper() or target in item["name"].upper())
        ]
        matches.sort(
            key=lambda item: (
                item["tradingsymbol"].upper() != target,
                not item["tradingsymbol"].upper().startswith(target),
                item["tradingsymbol"],
            )
        )
        return matches[:20]

    def resolve_instrument(self, exchange: str, tradingsymbol: str) -> dict[str, Any]:
        target = tradingsymbol.strip().upper()
        for item in self.search_instruments(target, exchange):
            if item["tradingsymbol"].upper() == target:
                return item
        raise KiteUnavailable(
            f"No exact {exchange.upper()}:{target} instrument was found on Dhan."
        )

    def previous_session_close(
        self, instrument_token: str, trading_date: date
    ) -> tuple[date, Decimal]:
        segment, security_id = decode_instrument(instrument_token)
        with self._lock:
            wait = 0.22 - (time_module.monotonic() - self._last_historical_request)
            if wait > 0:
                time_module.sleep(wait)
            self._last_historical_request = time_module.monotonic()
        body = json.dumps(
            {
                "securityId": security_id,
                "exchangeSegment": segment,
                "instrument": "EQUITY",
                "interval": "1",
                "oi": False,
                "fromDate": f"{trading_date - timedelta(days=14)} 09:15:00",
                "toDate": f"{trading_date} 00:00:00",
            }
        ).encode()
        request = Request(
            f"{API_URL}/charts/intraday",
            data=body,
            method="POST",
            headers={
                "access-token": self.require_access_token(),
                "client-id": self.client_id,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return previous_close_from_intraday(self._request_json(request), trading_date)

    def india_vix_previous_close(self, trading_date: date) -> tuple[date, Decimal]:
        security_id = self._find_india_vix_security_id()
        with self._lock:
            wait = 0.22 - (time_module.monotonic() - self._last_historical_request)
            if wait > 0:
                time_module.sleep(wait)
            self._last_historical_request = time_module.monotonic()
        body = json.dumps(
            {
                "securityId": security_id,
                "exchangeSegment": "IDX_I",
                "instrument": "INDEX",
                "interval": "1",
                "oi": False,
                "fromDate": f"{trading_date - timedelta(days=14)} 09:15:00",
                "toDate": f"{trading_date} 00:00:00",
            }
        ).encode()
        request = Request(
            f"{API_URL}/charts/intraday",
            data=body,
            method="POST",
            headers={
                "access-token": self.require_access_token(),
                "client-id": self.client_id,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return previous_close_from_intraday(self._request_json(request), trading_date)

    def completed_candles(
        self, instrument_token: str, since: datetime, until: datetime
    ) -> list[Candle]:
        segment, security_id = decode_instrument(instrument_token)
        since = ensure_ist(since)
        until = ensure_ist(until)
        query_since = bucket_start(since) or since
        with self._lock:
            wait = 0.22 - (time_module.monotonic() - self._last_historical_request)
            if wait > 0:
                time_module.sleep(wait)
            self._last_historical_request = time_module.monotonic()
        body = json.dumps(
            {
                "securityId": security_id,
                "exchangeSegment": segment,
                "instrument": "EQUITY",
                "interval": "1",
                "oi": False,
                "fromDate": query_since.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": until.strftime("%Y-%m-%d %H:%M:%S"),
            }
        ).encode()
        request = Request(
            f"{API_URL}/charts/intraday",
            data=body,
            method="POST",
            headers={
                "access-token": self.require_access_token(),
                "client-id": self.client_id,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return completed_candles_from_intraday(
            self._request_json(request), instrument_token, since, until
        )

    def historical_candles(
        self, instrument_token: str, since: datetime, until: datetime
    ) -> list[Candle]:
        """Return archived one-minute rows aggregated into three-minute candles."""
        return self.completed_candles(instrument_token, since, until)


class DhanMonitor(ReliableMonitorMixin):
    provider = "dhan"

    def __init__(
        self,
        *,
        gateway: DhanGateway,
        store: Store,
        finalization_delay_seconds: int,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.finalization_delay_seconds = finalization_delay_seconds
        self.on_event = on_event or (lambda event: None)
        self.aggregator = CandleAggregator()
        self.socket: Any | None = None
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
        self._socket_thread: threading.Thread | None = None
        self._finalizer_thread: threading.Thread | None = None
        self._generation = 0
        self._init_reliability()

    @staticmethod
    def subscription_messages(tokens: list[int | str]) -> list[dict[str, Any]]:
        instruments = []
        for token in sorted(set(str(value) for value in tokens)):
            segment, security_id = decode_instrument(token)
            instruments.append({"ExchangeSegment": segment, "SecurityId": security_id})
        return [
            {
                "RequestCode": 15,
                "InstrumentCount": len(instruments[start : start + 100]),
                "InstrumentList": instruments[start : start + 100],
            }
            for start in range(0, len(instruments), 100)
        ]

    def start(self, access_token: str) -> None:
        if not access_token or not self.gateway.client_id:
            raise KiteUnavailable("Dhan client ID or access token is missing.")
        try:
            import websocket  # noqa: F401
        except ImportError as exc:
            raise KiteUnavailable(
                "websocket-client is not installed. Run setup_windows.bat again."
            ) from exc
        self.stop()
        with self._lock:
            self.running = True
            self.last_error = None
            self.aggregator = CandleAggregator()
            self.last_tick_at = None
            self.connected_at = None
            self._generation += 1
            generation = self._generation
            self._socket_thread = threading.Thread(
                target=self._socket_loop,
                args=(access_token, generation),
                name="trade-m-dhan-websocket",
                daemon=True,
            )
            self._finalizer_thread = threading.Thread(
                target=self._finalizer_loop,
                args=(generation,),
                name="trade-m-dhan-candle-finalizer",
                daemon=True,
            )
            self._socket_thread.start()
            self._finalizer_thread.start()
            self._start_reliability_workers(generation)

    def stop(self) -> None:
        with self._lock:
            self.running = False
            self._generation += 1
            socket = self.socket
            self.socket = None
            self.connected = False
            self._wake_reliability_workers()
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _socket_loop(self, access_token: str, generation: int) -> None:
        import websocket

        url = (
            "wss://api-feed.dhan.co?"
            + urlencode(
                {
                    "version": "2",
                    "token": access_token,
                    "clientId": self.gateway.client_id,
                    "authType": "2",
                }
            )
        )
        while self.running and self._generation == generation:
            app = websocket.WebSocketApp(
                url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._lock:
                if not self.running or self._generation != generation:
                    return
                self.socket = app
            try:
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                self._record_error(f"Dhan WebSocket loop recovered from: {exc}")
            if self.running and self._generation == generation:
                time_module.sleep(5)

    def subscribe(self, tokens: list[int | str]) -> None:
        self._request_subscriptions(tokens)

    def _coerce_token(self, token: int | str) -> str:
        return str(token)

    def _subscribe_batches(self, tokens: set[Any]) -> set[Any]:
        ordered = sorted(str(token) for token in tokens)
        with self._lock:
            socket = self.socket
            connected = self.connected
        if socket is None or not connected:
            return set()
        subscribed: set[Any] = set()
        for start in range(0, len(ordered), 100):
            batch = ordered[start : start + 100]
            message = self.subscription_messages(batch)[0]
            try:
                socket.send(json.dumps(message))
                subscribed.update(batch)
            except Exception as exc:
                self._record_error(f"Dhan subscription failed: {exc}")
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

    def _on_open(self, socket: Any) -> None:
        with self._lock:
            if not self.running or socket is not self.socket:
                return
            self.connected = True
            self.connected_at = datetime.now(IST)
            self.last_error = None
        self._connection_ready()

    def _on_message(self, _socket: Any, message: bytes | str) -> None:
        with self._lock:
            if _socket is not None and _socket is not self.socket:
                return
        if not isinstance(message, bytes):
            return
        parsed = parse_ticker_packet(message)
        if parsed is None:
            if message and message[0] == 50:
                with self._lock:
                    self.last_error = "Dhan closed the feed; check data-plan and session access."
            return
        token, price, timestamp = parsed
        with self._lock:
            self.last_prices[token] = price
            self.last_tick_at = datetime.now(IST)
            completed = self.aggregator.add_tick(token, price, timestamp)
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
                self._record_error(f"Dhan candle finalizer recovered from: {exc}")
            time_module.sleep(0.5)

    def _process(self, candles: list[Candle]) -> None:
        self._process_reliably(candles)

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.error_count += 1
            self.last_error = message
        logger.warning(message)

    def _on_error(self, _socket: Any, error: Any) -> None:
        with self._lock:
            if _socket is not self.socket:
                return
        self._record_error(f"Dhan WebSocket error: {error}")

    def _on_close(self, _socket: Any, code: int | None, reason: str | None) -> None:
        with self._lock:
            if _socket is not self.socket:
                return
            self.connected = False
            self.disconnected_at = self.disconnected_at or datetime.now(IST)
            if self.running:
                self.last_error = f"Dhan WebSocket closed ({code}): {reason or 'no reason'}"
        self._connection_lost()
