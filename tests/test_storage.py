from datetime import date, datetime
from decimal import Decimal

from trade_m.domain import Candle, IST, calculate_levels
from trade_m.storage import Store


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


def add_example_rule(store: Store) -> int:
    reference = Decimal("365.20")
    upper, lower = calculate_levels(reference, Decimal("1.43"))
    return store.upsert_rule(
        exchange="NSE",
        tradingsymbol="TATAMOTORS",
        instrument_token=884737,
        trading_date=date(2026, 9, 15),
        percentage=Decimal("1.43"),
        reference_date=date(2026, 9, 14),
        reference_close=reference,
        upper_level=upper,
        lower_level=lower,
    )


def candle(low: str, high: str, *, opening: str = "372") -> Candle:
    return Candle.from_ohlc(
        instrument_token=884737,
        start=datetime(2026, 9, 15, 9, 39, tzinfo=IST),
        end=datetime(2026, 9, 15, 9, 42, tzinfo=IST),
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(low),
    )


def test_gap_above_does_not_create_event(tmp_path) -> None:
    store = make_store(tmp_path)
    add_example_rule(store)
    assert store.evaluate_candle(candle("376", "378")) == []


def test_crossing_creates_one_event_and_sets_state(tmp_path) -> None:
    store = make_store(tmp_path)
    rule_id = add_example_rule(store)
    created = store.evaluate_candle(candle("369", "372"))
    assert len(created) == 1
    assert created[0]["direction"] == "UPPER"
    assert created[0]["threshold"] == "370.422360"
    assert store.get_rule(rule_id)["upper_sent"] is True


def test_upper_level_only_alerts_on_downward_retracement(tmp_path) -> None:
    store = make_store(tmp_path)
    add_example_rule(store)
    upward_only = Candle.from_tick(
        884737, Decimal("369"), datetime(2026, 9, 15, 9, 39, tzinfo=IST)
    )
    upward_only.update(Decimal("372"))
    assert store.evaluate_candle(upward_only) == []


def test_lower_level_only_alerts_on_upward_retracement(tmp_path) -> None:
    store = make_store(tmp_path)
    add_example_rule(store)
    downward_only = Candle.from_tick(
        884737, Decimal("362"), datetime(2026, 9, 15, 9, 39, tzinfo=IST)
    )
    downward_only.update(Decimal("358"))
    assert store.evaluate_candle(downward_only) == []

    upward_retrace = Candle.from_tick(
        884737, Decimal("358"), datetime(2026, 9, 15, 9, 42, tzinfo=IST)
    )
    upward_retrace.update(Decimal("361"))
    created = store.evaluate_candle(upward_retrace)
    assert len(created) == 1
    assert created[0]["direction"] == "LOWER"


def test_same_candle_is_idempotent(tmp_path) -> None:
    store = make_store(tmp_path)
    add_example_rule(store)
    crossing = candle("369", "372")
    assert len(store.evaluate_candle(crossing)) == 1
    assert store.evaluate_candle(crossing) == []
    assert len(store.events_after()) == 1
    assert store.latest_event_id() == 1
    assert len(store.events_after(trading_date=date(2026, 9, 15))) == 1
    assert store.events_after(trading_date=date(2026, 9, 16)) == []


def test_rule_update_resets_directional_state(tmp_path) -> None:
    store = make_store(tmp_path)
    rule_id = add_example_rule(store)
    store.evaluate_candle(candle("369", "372"))
    upper, lower = calculate_levels(Decimal("365.20"), Decimal("2"))
    same_id = store.upsert_rule(
        exchange="NSE",
        tradingsymbol="TATAMOTORS",
        instrument_token=884737,
        trading_date=date(2026, 9, 15),
        percentage=Decimal("2"),
        reference_date=date(2026, 9, 14),
        reference_close=Decimal("365.20"),
        upper_level=upper,
        lower_level=lower,
    )
    assert same_id == rule_id
    rule = store.get_rule(rule_id)
    assert rule["percentage"] == "2"
    assert rule["upper_sent"] is False


def test_provider_isolates_same_instrument_identifier(tmp_path) -> None:
    store = make_store(tmp_path)
    add_example_rule(store)
    upper, lower = calculate_levels(Decimal("365.20"), Decimal("1.43"))
    store.upsert_rule(
        exchange="NSE",
        tradingsymbol="UPSTOXTEST",
        instrument_token=884737,
        trading_date=date(2026, 9, 15),
        percentage=Decimal("1.43"),
        reference_date=date(2026, 9, 14),
        reference_close=Decimal("365.20"),
        upper_level=upper,
        lower_level=lower,
        provider="upstox",
    )
    created = store.evaluate_candle(candle("369", "372"), provider="upstox")
    assert [event["tradingsymbol"] for event in created] == ["UPSTOXTEST"]


def test_percentage_edit_and_bulk_pause(tmp_path) -> None:
    store = make_store(tmp_path)
    rule_id = add_example_rule(store)
    updated = store.update_rule_percentage(rule_id, Decimal("2"))
    assert updated is not None
    assert updated["percentage"] == "2"
    assert updated["upper_level"] == "372.5040"
    assert store.set_rules_active(date(2026, 9, 15), False) == 1
    assert store.daily_rules(date(2026, 9, 15))[0]["active"] is False
    assert store.active_rules(date(2026, 9, 15)) == []
