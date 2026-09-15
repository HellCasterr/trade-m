from datetime import datetime
from decimal import Decimal

from trade_m.domain import Candle, CandleAggregator, IST, bucket_start, calculate_levels


def test_levels_match_worked_example() -> None:
    upper, lower = calculate_levels(Decimal("365.20"), Decimal("1.43"))
    assert upper == Decimal("370.422360")
    assert lower == Decimal("359.977640")


def test_opening_gap_does_not_count_as_touch() -> None:
    candle = Candle(
        instrument_token=1,
        start=datetime(2026, 9, 15, 9, 15, tzinfo=IST),
        end=datetime(2026, 9, 15, 9, 18, tzinfo=IST),
        open=Decimal("377"),
        high=Decimal("378"),
        low=Decimal("376"),
        close=Decimal("377.50"),
    )
    assert not candle.contains(Decimal("370.422360"))


def test_0942_candle_crossing_threshold_counts_as_touch() -> None:
    candle = Candle.from_ohlc(
        instrument_token=1,
        start=datetime(2026, 9, 15, 9, 39, tzinfo=IST),
        end=datetime(2026, 9, 15, 9, 42, tzinfo=IST),
        open=Decimal("372"),
        high=Decimal("372.50"),
        low=Decimal("369"),
        close=Decimal("369.20"),
    )
    assert candle.contains(Decimal("370.422360"))
    assert candle.crossed_down(Decimal("370.422360"))
    assert not candle.crossed_up(Decimal("370.422360"))


def test_buckets_are_aligned_from_0915() -> None:
    assert bucket_start(datetime(2026, 9, 15, 9, 41, 59, tzinfo=IST)) == datetime(
        2026, 9, 15, 9, 39, tzinfo=IST
    )
    assert bucket_start(datetime(2026, 9, 15, 9, 42, tzinfo=IST)) == datetime(
        2026, 9, 15, 9, 42, tzinfo=IST
    )


def test_aggregator_finalizes_previous_bucket_on_next_tick() -> None:
    aggregator = CandleAggregator()
    aggregator.add_tick(1, Decimal("372"), datetime(2026, 9, 15, 9, 39, tzinfo=IST))
    aggregator.add_tick(1, Decimal("369"), datetime(2026, 9, 15, 9, 41, tzinfo=IST))
    completed = aggregator.add_tick(
        1, Decimal("369.10"), datetime(2026, 9, 15, 9, 42, tzinfo=IST)
    )
    assert len(completed) == 1
    assert completed[0].start.hour == 9 and completed[0].start.minute == 39
    assert completed[0].high == Decimal("372")
    assert completed[0].low == Decimal("369")
    assert completed[0].crossed_down(Decimal("370.422360"))


def test_aggregator_carries_direction_across_bucket_boundary() -> None:
    aggregator = CandleAggregator()
    aggregator.add_tick(1, Decimal("372"), datetime(2026, 9, 15, 9, 41, tzinfo=IST))
    aggregator.add_tick(1, Decimal("369"), datetime(2026, 9, 15, 9, 42, tzinfo=IST))
    completed = aggregator.finalize_due(datetime(2026, 9, 15, 9, 45, 4, tzinfo=IST))
    assert len(completed) == 1
    assert completed[0].crossed_down(Decimal("370.422360"))


def test_aggregator_does_not_treat_overnight_gap_as_retracement() -> None:
    aggregator = CandleAggregator()
    aggregator.add_tick(1, Decimal("365"), datetime(2026, 9, 14, 15, 29, tzinfo=IST))
    aggregator.finalize_due(datetime(2026, 9, 14, 15, 33, tzinfo=IST))
    aggregator.add_tick(1, Decimal("377"), datetime(2026, 9, 15, 9, 15, tzinfo=IST))
    completed = aggregator.finalize_due(datetime(2026, 9, 15, 9, 18, 4, tzinfo=IST))
    assert len(completed) == 1
    assert not completed[0].crossed_up(Decimal("370.422360"))
