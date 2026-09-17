import json
import struct
from datetime import UTC, date, datetime
from decimal import Decimal

from trade_m.dhan_service import (
    DhanGateway,
    DhanMonitor,
    completed_candles_from_intraday,
    decode_instrument,
    encode_instrument,
    parse_ticker_packet,
    previous_close_from_intraday,
)
from trade_m.domain import IST
from trade_m.kite_service import KiteGateway, LiveMonitor
from trade_m.storage import Store
from trade_m.upstox_service import UpstoxGateway, UpstoxMonitor
import trade_m.upstox_service as upstox_service


class FakeKiteTicker:
    MODE_FULL = "full"

    def __init__(self) -> None:
        self.subscriptions = []
        self.modes = []

    def subscribe(self, tokens):
        self.subscriptions.append(tokens)

    def set_mode(self, mode, tokens):
        self.modes.append((mode, tokens))


def test_zerodha_subscriptions_are_batched(tmp_path) -> None:
    monitor = LiveMonitor(
        api_key="key",
        store=Store(tmp_path / "batch.db"),
        finalization_delay_seconds=3,
    )
    ticker = FakeKiteTicker()
    monitor.ticker = ticker
    monitor.connected = True
    monitor.subscribe(list(range(1, 1202)))
    assert [len(batch) for batch in ticker.subscriptions] == [500, 500, 201]


def test_zerodha_india_vix_resolves_index_instrument(monkeypatch) -> None:
    gateway = KiteGateway("key", "secret")
    gateway._instruments["NSE"] = [
        {"tradingsymbol": "INDIA VIX", "instrument_token": 264969}
    ]
    captured = {}

    def previous_close(token, trading_date):
        captured["token"] = token
        return date(2026, 9, 16), Decimal("12.990")

    monkeypatch.setattr(gateway, "previous_session_close", previous_close)
    result = gateway.india_vix_previous_close(date(2026, 9, 17))
    assert captured["token"] == 264969
    assert result == (date(2026, 9, 16), Decimal("12.990"))


def test_upstox_instrument_record_is_normalised() -> None:
    result = UpstoxGateway._normalise_instrument(
        {
            "trading_symbol": "RELIANCE",
            "name": "Reliance Industries",
            "instrument_key": "NSE_EQ|INE002A01018",
        },
        "NSE",
    )
    assert result["tradingsymbol"] == "RELIANCE"
    assert result["instrument_token"] == "NSE_EQ|INE002A01018"


def test_upstox_token_exchange_uses_requests(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"access_token": "access", "user_name": "Test user"}

    class FakeConfiguration:
        access_token = None

    class FakeSdk:
        Configuration = FakeConfiguration

        @staticmethod
        def ApiClient(configuration):
            return configuration

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(upstox_service.requests, "post", fake_post)
    monkeypatch.setattr(upstox_service, "_upstox_module", lambda: FakeSdk)
    gateway = UpstoxGateway("api-key", "api-secret", "http://localhost/callback")
    gateway.authenticate("single-use-code")

    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["redirect_uri"] == "http://localhost/callback"
    assert gateway.authenticated


def test_upstox_ltpc_message_updates_aggregator(tmp_path) -> None:
    gateway = UpstoxGateway("key", "secret", "http://localhost/callback")
    monitor = UpstoxMonitor(
        gateway=gateway,
        store=Store(tmp_path / "upstox.db"),
        finalization_delay_seconds=3,
    )
    timestamp = datetime(2026, 9, 15, 9, 16, tzinfo=IST)
    monitor._on_message(
        {
            "feeds": {
                "NSE_EQ|INE002A01018": {
                    "ltpc": {
                        "ltp": 1400.25,
                        "ltt": str(int(timestamp.timestamp() * 1000)),
                    }
                }
            }
        }
    )
    assert monitor.last_prices["NSE_EQ|INE002A01018"] == Decimal("1400.25")


def test_upstox_recovery_uses_intraday_v3(monkeypatch) -> None:
    called = {}

    class Result:
        class data:
            candles = [
                ["2026-09-15T09:39:00+05:30", 372, 372.5, 369, 369.2, 100, 0]
            ]

    class History:
        def __init__(self, client):
            called["client"] = client

        def get_intra_day_candle_data(self, token, unit, interval):
            called["args"] = (token, unit, interval)
            return Result()

    class FakeSdk:
        HistoryV3Api = History

    gateway = UpstoxGateway("key", "secret", "http://localhost/callback")
    gateway.api_client = object()
    monkeypatch.setattr(upstox_service, "_upstox_module", lambda: FakeSdk)
    candles = gateway.completed_candles(
        "NSE_EQ|INE002A01018",
        datetime(2026, 9, 15, 9, 40, tzinfo=IST),
        datetime(2026, 9, 15, 9, 42, 3, tzinfo=IST),
    )
    assert called["args"] == ("NSE_EQ|INE002A01018", "minutes", "3")
    assert len(candles) == 1
    assert candles[0].crossed_down(Decimal("370.422360"))


def test_upstox_india_vix_uses_index_instrument_key(monkeypatch) -> None:
    gateway = UpstoxGateway("key", "secret", "http://localhost/callback")
    captured = {}

    def previous_close(token, trading_date):
        captured["token"] = token
        return date(2026, 9, 16), Decimal("12.990")

    monkeypatch.setattr(gateway, "previous_session_close", previous_close)
    result = gateway.india_vix_previous_close(date(2026, 9, 17))
    assert captured["token"] == "NSE_INDEX|India VIX"
    assert result == (date(2026, 9, 16), Decimal("12.990"))


def test_dhan_token_round_trip_and_instrument_normalisation() -> None:
    token = encode_instrument("NSE_EQ", "1333")
    assert token == "NSE_EQ|1333"
    assert decode_instrument(token) == ("NSE_EQ", "1333")
    result = DhanGateway._normalise_instrument(
        {
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_SEGMENT": "E",
            "SEM_SMST_SECURITY_ID": "1333",
            "SEM_INSTRUMENT_NAME": "EQUITY",
            "SEM_TRADING_SYMBOL": "HDFCBANK",
            "SEM_CUSTOM_SYMBOL": "HDFC Bank Ltd",
            "SEM_SERIES": "EQ",
        }
    )
    assert result is not None
    assert result["tradingsymbol"] == "HDFCBANK"
    assert result["instrument_token"] == "NSE_EQ|1333"
    assert DhanGateway._index_security_id(
        {
            "SEM_TRADING_SYMBOL": "INDIA VIX",
            "SEM_SMST_SECURITY_ID": "21",
        },
        "INDIA VIX",
    ) == "21"


def test_dhan_subscription_messages_are_batched_at_100(tmp_path) -> None:
    gateway = DhanGateway("client", "", "", "token")
    monitor = DhanMonitor(
        gateway=gateway,
        store=Store(tmp_path / "dhan-batch.db"),
        finalization_delay_seconds=3,
    )
    messages = monitor.subscription_messages(
        [encode_instrument("NSE_EQ", value) for value in range(1, 251)]
    )
    assert [message["InstrumentCount"] for message in messages] == [100, 100, 50]
    assert all(message["RequestCode"] == 15 for message in messages)


def test_dhan_ticker_packet_is_parsed() -> None:
    timestamp = datetime(2026, 9, 15, 9, 16, tzinfo=IST)
    packet = struct.pack(
        "<BHBIfI", 2, 16, 1, 1333, 1400.25, int(timestamp.timestamp())
    )
    parsed = parse_ticker_packet(packet)
    assert parsed is not None
    token, price, tick_time = parsed
    assert token == "NSE_EQ|1333"
    assert price == Decimal("1400.25")
    assert tick_time == timestamp


def test_dhan_history_uses_final_aligned_three_minute_close() -> None:
    def epoch(hour: int, minute: int) -> int:
        return int(datetime(2026, 9, 14, hour, minute, tzinfo=IST).astimezone(UTC).timestamp())

    result = previous_close_from_intraday(
        {
            "timestamp": [epoch(15, 26), epoch(15, 27), epoch(15, 28), epoch(15, 29)],
            "close": [364.8, 365.0, 365.1, 365.2],
        },
        date(2026, 9, 15),
    )
    assert result == (date(2026, 9, 14), Decimal("365.2"))


def test_dhan_india_vix_uses_index_history_payload(monkeypatch) -> None:
    gateway = DhanGateway("client", "", "", "token")
    gateway.access_token = "token"
    captured = {}

    def epoch(hour: int, minute: int) -> int:
        value = datetime(2026, 9, 16, hour, minute, tzinfo=IST)
        return int(value.astimezone(UTC).timestamp())

    def request_json(request):
        captured.update(json.loads(request.data.decode()))
        return {
            "timestamp": [epoch(15, 27), epoch(15, 28), epoch(15, 29)],
            "close": [12.95, 12.97, 12.99],
        }

    monkeypatch.setattr(gateway, "_find_india_vix_security_id", lambda: "21")
    monkeypatch.setattr(gateway, "_request_json", request_json)
    result = gateway.india_vix_previous_close(date(2026, 9, 17))

    assert captured["securityId"] == "21"
    assert captured["exchangeSegment"] == "IDX_I"
    assert captured["instrument"] == "INDEX"
    assert result == (date(2026, 9, 16), Decimal("12.99"))


def test_dhan_recovery_builds_directional_three_minute_candle() -> None:
    def epoch(minute: int) -> int:
        value = datetime(2026, 9, 15, 9, minute, tzinfo=IST)
        return int(value.astimezone(UTC).timestamp())

    result = completed_candles_from_intraday(
        {
            "timestamp": [epoch(39), epoch(40), epoch(41)],
            "open": [372, 371, 370],
            "high": [372.5, 371.5, 370.5],
            "low": [371, 370, 369],
            "close": [371, 370, 369.2],
        },
        "NSE_EQ|1333",
        datetime(2026, 9, 15, 9, 38, tzinfo=IST),
        datetime(2026, 9, 15, 9, 42, tzinfo=IST),
    )
    assert len(result) == 1
    assert result[0].open == Decimal("372")
    assert result[0].close == Decimal("369.2")
    assert result[0].crossed_down(Decimal("370.422360"))


def test_monitor_candle_failure_is_contained(tmp_path) -> None:
    monitor = LiveMonitor(
        api_key="key",
        store=Store(tmp_path / "errors.db"),
        finalization_delay_seconds=3,
    )
    monitor._process([object()])  # type: ignore[list-item]
    assert monitor.error_count == 1
    assert "processing failed" in (monitor.last_error or "")


def test_dhan_binary_message_updates_aggregator(tmp_path) -> None:
    gateway = DhanGateway("client", "", "", "token")
    monitor = DhanMonitor(
        gateway=gateway,
        store=Store(tmp_path / "dhan.db"),
        finalization_delay_seconds=3,
    )
    timestamp = datetime(2026, 9, 15, 9, 16, tzinfo=IST)
    packet = struct.pack(
        "<BHBIfI", 2, 16, 1, 1333, 1400.25, int(timestamp.timestamp())
    )
    monitor._on_message(None, packet)
    assert monitor.last_prices["NSE_EQ|1333"] == Decimal("1400.25")
