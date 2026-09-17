import json
import struct
import time
from datetime import UTC, date, datetime
from decimal import Decimal

from trade_m import upstox_service
from trade_m.dhan_service import (
    DhanGateway,
    DhanMonitor,
    completed_candles_from_intraday,
    decode_instrument,
    encode_instrument,
    parse_ticker_packet,
    previous_close_from_intraday,
)
from trade_m.domain import IST, Candle, calculate_levels, session_bounds
from trade_m.kite_service import KiteGateway, LiveMonitor, create_daily_rule
from trade_m.storage import Store
from trade_m.upstox_service import UpstoxGateway, UpstoxMonitor


class FakeKiteTicker:
    MODE_FULL = "full"

    def __init__(self) -> None:
        self.subscriptions = []
        self.modes = []

    def subscribe(self, tokens):
        self.subscriptions.append(tokens)

    def set_mode(self, mode, tokens):
        self.modes.append((mode, tokens))


def add_today_rule(store: Store, token: int = 884737) -> int:
    today = datetime.now(IST).date()
    upper, lower = calculate_levels(Decimal("365.20"), Decimal("1.43"))
    return store.upsert_rule(
        exchange="NSE",
        tradingsymbol="TATAMOTORS",
        instrument_token=token,
        trading_date=today,
        percentage=Decimal("1.43"),
        reference_date=today,
        reference_close=Decimal("365.20"),
        upper_level=upper,
        lower_level=lower,
    )


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


def test_failed_subscription_is_retried_and_reported(tmp_path) -> None:
    class FlakyTicker(FakeKiteTicker):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def subscribe(self, tokens):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary failure")
            super().subscribe(tokens)

    store = Store(tmp_path / "subscription-retry.db")
    add_today_rule(store)
    monitor = LiveMonitor(
        api_key="key", store=store, finalization_delay_seconds=3
    )
    monitor.ticker = FlakyTicker()
    monitor.connected = True

    monitor._reconcile_subscriptions()
    assert monitor.status()["subscription_gap"] == 1

    monitor._reconcile_subscriptions()
    status = monitor.status()
    assert status["desired_subscriptions"] == 1
    assert status["subscribed_instruments"] == 1
    assert status["subscription_gap"] == 0


def test_fresh_start_recovers_from_session_open_and_saves_checkpoint(tmp_path) -> None:
    store = Store(tmp_path / "startup-recovery.db")
    token = 884737
    add_today_rule(store, token)
    today = datetime.now(IST).date()
    opening, _ = session_bounds(today)
    candle = Candle.from_tick(
        token,
        Decimal("372"),
        opening.replace(hour=9, minute=39),
    )
    candle.update(Decimal("369"))
    recovered_until = candle.end

    class RecoveryGateway:
        def __init__(self) -> None:
            self.calls = []

        def completed_candles(self, instrument_token, since, until):
            self.calls.append((instrument_token, since, until))
            return [candle]

    gateway = RecoveryGateway()
    events = []
    monitor = LiveMonitor(
        api_key="key",
        gateway=gateway,  # type: ignore[arg-type]
        store=store,
        finalization_delay_seconds=3,
        on_event=events.append,
    )
    monitor._completed_through = lambda now=None: recovered_until  # type: ignore[method-assign]
    monitor.running = True
    monitor._generation = 1
    monitor._start_reliability_workers(1)
    monitor._queue_recovery({token})

    deadline = time.monotonic() + 2
    while not gateway.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    while not events and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()

    assert gateway.calls[0][0] == token
    assert gateway.calls[0][1] == opening
    assert gateway.calls[0][2] == recovered_until
    assert len(events) == 1
    assert store.candle_checkpoint("zerodha", token, today) == recovered_until


def test_recovery_uses_independent_per_instrument_checkpoints(tmp_path) -> None:
    store = Store(tmp_path / "per-instrument-recovery.db")
    today = datetime.now(IST).date()
    opening, _ = session_bounds(today)
    first_checkpoint = opening.replace(hour=9, minute=30)
    second_checkpoint = opening.replace(hour=9, minute=36)
    recovered_until = opening.replace(hour=9, minute=42)
    store.advance_candle_checkpoint("zerodha", 101, today, first_checkpoint)
    store.advance_candle_checkpoint("zerodha", 202, today, second_checkpoint)

    monitor = LiveMonitor(
        api_key="key",
        gateway=object(),  # type: ignore[arg-type]
        store=store,
        finalization_delay_seconds=3,
    )
    monitor._completed_through = lambda now=None: recovered_until  # type: ignore[method-assign]
    monitor._queue_recovery({101, 202})

    assert monitor._recovery_requests[101][0] == first_checkpoint
    assert monitor._recovery_requests[202][0] == second_checkpoint


def test_failed_recovery_keeps_gap_checkpoint_for_retry(tmp_path) -> None:
    store = Store(tmp_path / "failed-recovery.db")
    token = 884737
    add_today_rule(store, token)
    today = datetime.now(IST).date()
    opening, _ = session_bounds(today)
    checkpoint = opening.replace(hour=9, minute=30)
    store.advance_candle_checkpoint("zerodha", token, today, checkpoint)
    pending = Candle.from_tick(
        token, Decimal("372"), opening.replace(hour=9, minute=39)
    )
    pending.update(Decimal("369"))
    events = []
    monitor = LiveMonitor(
        api_key="key",
        gateway=object(),  # type: ignore[arg-type]
        store=store,
        finalization_delay_seconds=3,
        on_event=events.append,
    )
    monitor._recovering_tokens.add(token)
    monitor._gapped_tokens.add(token)
    monitor._pending_candles[token].append(pending)

    monitor._finish_recovery(
        token,
        success=False,
        completed_through=None,
        retry_since=checkpoint,
    )

    assert len(events) == 1
    assert store.candle_checkpoint("zerodha", token, today) == checkpoint
    assert monitor._recovery_requests[token][0] == checkpoint
    assert token in monitor._gapped_tokens


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


def test_zerodha_stop_history_returns_three_minute_candles() -> None:
    called = {}

    class Client:
        @staticmethod
        def historical_data(token, since, until, interval, **kwargs):
            called["args"] = (token, since, until, interval, kwargs)
            return [
                {
                    "date": datetime(2026, 9, 14, 15, 27, tzinfo=IST),
                    "open": 364,
                    "high": 366,
                    "low": 363,
                    "close": 365.2,
                }
            ]

    gateway = KiteGateway("key", "secret")
    gateway.kite = Client()
    candles = gateway.historical_candles(
        884737,
        datetime(2026, 8, 18, 0, 0, tzinfo=IST),
        datetime(2026, 9, 15, 0, 0, tzinfo=IST),
    )
    assert called["args"][0] == 884737
    assert called["args"][3] == "3minute"
    assert len(candles) == 1
    assert candles[0].close == Decimal("365.2")


def test_rule_creation_uses_preloaded_history_for_reference_and_stops(tmp_path) -> None:
    trading_date = date(2026, 9, 15)
    historical = Candle.from_ohlc(
        instrument_token=884737,
        start=datetime(2026, 9, 14, 15, 27, tzinfo=IST),
        end=datetime(2026, 9, 14, 15, 30, tzinfo=IST),
        open=Decimal("364"),
        high=Decimal("366"),
        low=Decimal("363"),
        close=Decimal("365.20"),
    )

    class Gateway:
        provider = "zerodha"

        @staticmethod
        def resolve_instrument(exchange, tradingsymbol):
            return {
                "instrument_token": 884737,
                "tradingsymbol": tradingsymbol,
            }

        @staticmethod
        def historical_candles(instrument_token, since, until):
            assert instrument_token == 884737
            assert since.date() == date(2026, 8, 18)
            assert until.date() == trading_date
            return [historical]

        @staticmethod
        def previous_session_close(instrument_token, value):
            raise AssertionError("history preload should supply the reference close")

    store = Store(tmp_path / "preload.db")
    rule = create_daily_rule(
        Gateway(),
        store,
        exchange="NSE",
        tradingsymbol="TATAMOTORS",
        percentage=Decimal("1.43"),
        trading_date=trading_date,
    )
    assert rule["reference_close"] == "365.20"
    with store.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]
    assert count == 1


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


def test_upstox_stop_history_uses_historical_v3(monkeypatch) -> None:
    called = {}

    class Result:
        class data:
            candles = [
                ["2026-09-14T15:27:00+05:30", 364, 366, 363, 365.2, 100, 0]
            ]

    class History:
        def __init__(self, client):
            called["client"] = client

        def get_historical_candle_data1(
            self, token, unit, interval, to_date, from_date
        ):
            called["args"] = (token, unit, interval, to_date, from_date)
            return Result()

    class FakeSdk:
        HistoryV3Api = History

    gateway = UpstoxGateway("key", "secret", "http://localhost/callback")
    gateway.api_client = object()
    monkeypatch.setattr(upstox_service, "_upstox_module", lambda: FakeSdk)
    candles = gateway.historical_candles(
        "NSE_EQ|INE002A01018",
        datetime(2026, 8, 18, 0, 0, tzinfo=IST),
        datetime(2026, 9, 15, 0, 0, tzinfo=IST),
    )
    assert called["args"] == (
        "NSE_EQ|INE002A01018",
        "minutes",
        "3",
        "2026-09-15",
        "2026-08-18",
    )
    assert len(candles) == 1
    assert candles[0].close == Decimal("365.2")


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


def test_dhan_rejects_incomplete_historical_arrays() -> None:
    payload = {
        "timestamp": [int(datetime(2026, 9, 15, 9, 39, tzinfo=IST).timestamp())],
        "open": [372],
        "high": [373],
        "low": [],
        "close": [369],
    }
    try:
        completed_candles_from_intraday(
            payload,
            "NSE_EQ|1333",
            datetime(2026, 9, 15, 9, 15, tzinfo=IST),
            datetime(2026, 9, 15, 9, 42, tzinfo=IST),
        )
    except RuntimeError as exc:
        assert "incomplete historical OHLC" in str(exc)
    else:
        raise AssertionError("Incomplete Dhan columns should not be silently truncated")


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
