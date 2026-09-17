from datetime import date, datetime, timedelta
from decimal import Decimal

from trade_m.domain import IST, Candle, calculate_levels
from trade_m.stop_loss import calculate_stop_loss
from trade_m.storage import Store

TOKEN = 884737


def bar(
    session_date: date,
    minute: int,
    opening: str,
    high: str,
    low: str,
    close: str,
) -> Candle:
    start = datetime.combine(session_date, datetime.min.time(), IST).replace(
        hour=9, minute=minute
    )
    return Candle.from_ohlc(
        instrument_token=TOKEN,
        start=start,
        end=start + timedelta(minutes=3),
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def test_fallback_stop_is_on_protective_side_for_each_alert_direction() -> None:
    session = date(2026, 9, 15)
    upper_alert = bar(session, 39, "102", "102", "100.8", "101")
    long_stop = calculate_stop_loss(
        history=[upper_alert],
        alert_candle=upper_alert,
        percentage=Decimal("1"),
        direction="UPPER",
    )
    assert long_stop.trade_side == "LONG"
    assert long_stop.stop_price < long_stop.entry_price
    assert long_stop.confidence == "UNVALIDATED"

    lower_alert = bar(session, 42, "98", "99.2", "98", "99")
    short_stop = calculate_stop_loss(
        history=[lower_alert],
        alert_candle=lower_alert,
        percentage=Decimal("1"),
        direction="LOWER",
    )
    assert short_stop.trade_side == "SHORT"
    assert short_stop.stop_price > short_stop.entry_price


def test_stop_calculation_ignores_candles_after_alert_close() -> None:
    session = date(2026, 9, 15)
    alert = bar(session, 39, "102", "102", "100.8", "101")
    future_extreme = bar(session, 42, "101", "150", "50", "100")
    without_future = calculate_stop_loss(
        history=[alert],
        alert_candle=alert,
        percentage=Decimal("1"),
        direction="UPPER",
    )
    with_future = calculate_stop_loss(
        history=[alert, future_extreme],
        alert_candle=alert,
        percentage=Decimal("1"),
        direction="UPPER",
    )
    assert with_future.stop_price == without_future.stop_price
    assert with_future.atr == without_future.atr


def test_walk_forward_uses_earlier_alerts_and_later_validation() -> None:
    first = date(2026, 8, 1)
    history: list[Candle] = []
    for offset in range(12):
        session = first + timedelta(days=offset)
        history.extend(
            (
                bar(session, 15, "102", "102", "100.9", "101"),
                bar(session, 18, "101", "105", "100.8", "100"),
            )
        )
    alert = bar(first + timedelta(days=12), 15, "102", "102", "100.9", "101")
    result = calculate_stop_loss(
        history=[*history, alert],
        alert_candle=alert,
        percentage=Decimal("1"),
        direction="UPPER",
    )
    assert result.backtest_samples == 11
    assert result.validation_samples == 4
    assert result.validation_win_rate == Decimal("100")
    assert result.confidence in {"MEDIUM", "HIGH"}
    assert result.method == "Walk-forward ATR + swing"


def test_event_persists_stop_before_it_is_returned(tmp_path) -> None:
    store = Store(tmp_path / "stops.db")
    reference = Decimal("100")
    upper, lower = calculate_levels(reference, Decimal("1"))
    store.upsert_rule(
        exchange="NSE",
        tradingsymbol="EXAMPLE",
        instrument_token=TOKEN,
        trading_date=date(2026, 9, 15),
        percentage=Decimal("1"),
        reference_date=date(2026, 9, 14),
        reference_close=reference,
        upper_level=upper,
        lower_level=lower,
    )
    alert = bar(date(2026, 9, 15), 39, "102", "102", "100.8", "101")
    created = store.evaluate_candle(alert)
    assert len(created) == 1
    assert created[0]["trade_side"] == "LONG"
    assert Decimal(created[0]["stop_loss"]) < Decimal(created[0]["entry_price"])
    persisted = store.events_after()[0]
    assert persisted["stop_loss"] == created[0]["stop_loss"]
    assert persisted["stop_loss_display"] == created[0]["stop_loss_display"]


def test_bad_history_degrades_to_stop_fallback_without_losing_alert(tmp_path) -> None:
    store = Store(tmp_path / "bad-history.db")
    reference = Decimal("100")
    upper, lower = calculate_levels(reference, Decimal("1"))
    store.upsert_rule(
        exchange="NSE",
        tradingsymbol="EXAMPLE",
        instrument_token=TOKEN,
        trading_date=date(2026, 9, 15),
        percentage=Decimal("1"),
        reference_date=date(2026, 9, 14),
        reference_close=reference,
        upper_level=upper,
        lower_level=lower,
    )
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO market_candles (
                provider, instrument_token, candle_start, candle_end,
                candle_open, candle_high, candle_low, candle_close
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "zerodha",
                str(TOKEN),
                "2026-09-14T15:27:00+05:30",
                "2026-09-14T15:30:00+05:30",
                "invalid",
                "101",
                "99",
                "100",
            ),
        )
    alert = bar(date(2026, 9, 15), 39, "102", "102", "100.8", "101")
    created = store.evaluate_candle(alert)
    assert len(created) == 1
    assert created[0]["stop_method"] == "Current-candle safety fallback"
    assert created[0]["stop_confidence"] == "UNVALIDATED"
