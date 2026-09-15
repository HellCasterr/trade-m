import struct
from datetime import UTC, date, datetime
from decimal import Decimal

from trade_m.dhan_service import (
    DhanGateway,
    DhanMonitor,
    decode_instrument,
    encode_instrument,
    parse_ticker_packet,
    previous_close_from_intraday,
)
from trade_m.domain import IST
from trade_m.kite_service import LiveMonitor
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
