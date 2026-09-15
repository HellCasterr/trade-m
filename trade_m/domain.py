from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import TypeAlias
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
THREE_MINUTES = timedelta(minutes=3)
DISPLAY_QUANTUM = Decimal("0.01")
InstrumentKey: TypeAlias = int | str


def as_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def calculate_levels(reference_close: Decimal, percentage: Decimal) -> tuple[Decimal, Decimal]:
    if reference_close <= 0:
        raise ValueError("Reference close must be greater than zero")
    if percentage <= 0:
        raise ValueError("Percentage must be greater than zero")
    factor = percentage / Decimal("100")
    return reference_close * (Decimal("1") + factor), reference_close * (
        Decimal("1") - factor
    )


def display_price(value: Decimal) -> str:
    return str(value.quantize(DISPLAY_QUANTUM, rounding=ROUND_HALF_UP))


def ensure_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def session_bounds(trading_date: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(trading_date, SESSION_OPEN, IST),
        datetime.combine(trading_date, SESSION_CLOSE, IST),
    )


def market_session_open(now: datetime | None = None) -> bool:
    now = ensure_ist(now or datetime.now(IST))
    return (
        now.weekday() < 5
        and SESSION_OPEN <= now.time().replace(tzinfo=None) < SESSION_CLOSE
    )


def bucket_start(timestamp: datetime) -> datetime | None:
    timestamp = ensure_ist(timestamp)
    opening, closing = session_bounds(timestamp.date())
    if timestamp < opening or timestamp >= closing:
        return None
    offset_seconds = int((timestamp - opening).total_seconds())
    return opening + timedelta(seconds=(offset_seconds // 180) * 180)


@dataclass
class Candle:
    instrument_token: InstrumentKey
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    downward_moves: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    upward_moves: list[tuple[Decimal, Decimal]] = field(default_factory=list)

    @classmethod
    def from_tick(
        cls,
        instrument_token: InstrumentKey,
        price: Decimal,
        timestamp: datetime,
        previous_price: Decimal | None = None,
    ) -> "Candle":
        start = bucket_start(timestamp)
        if start is None:
            raise ValueError("Tick is outside the regular market session")
        candle = cls(
            instrument_token=instrument_token,
            start=start,
            end=start + THREE_MINUTES,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        if previous_price is not None:
            candle.record_move(previous_price, price)
        return candle

    @classmethod
    def from_ohlc(
        cls,
        *,
        instrument_token: InstrumentKey,
        start: datetime,
        end: datetime,
        open: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
    ) -> "Candle":
        """Build a recovered candle with directionally provable price moves.

        OHLC data does not reveal the intrabar tick order. It does prove that price
        moved from the open down to the low and from the open up to the high, which
        is sufficient for conservative directional retracement checks.
        """
        candle = cls(
            instrument_token=instrument_token,
            start=ensure_ist(start),
            end=ensure_ist(end),
            open=open,
            high=high,
            low=low,
            close=close,
        )
        candle.record_move(open, low)
        candle.record_move(open, high)
        return candle

    def update(self, price: Decimal) -> None:
        self.record_move(self.close, price)
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

    def record_move(self, previous: Decimal, current: Decimal) -> None:
        if current < previous:
            self.downward_moves.append((current, previous))
        elif current > previous:
            self.upward_moves.append((previous, current))

    def crossed_down(self, level: Decimal) -> bool:
        return any(low <= level <= high for low, high in self.downward_moves)

    def crossed_up(self, level: Decimal) -> bool:
        return any(low <= level <= high for low, high in self.upward_moves)

    def contains(self, level: Decimal) -> bool:
        return self.low <= level <= self.high


class CandleAggregator:
    """Aggregates exchange-timestamped ticks into aligned three-minute candles."""

    def __init__(self) -> None:
        self._current: dict[InstrumentKey, Candle] = {}
        self._last_finalized_end: dict[InstrumentKey, datetime] = {}
        self._last_price: dict[InstrumentKey, Decimal] = {}
        self._last_tick_at: dict[InstrumentKey, datetime] = {}

    def add_tick(
        self, instrument_token: InstrumentKey, price: Decimal, timestamp: datetime
    ) -> list[Candle]:
        start = bucket_start(timestamp)
        if start is None:
            return []
        last_end = self._last_finalized_end.get(instrument_token)
        if last_end is not None and start + THREE_MINUTES <= last_end:
            return []

        timestamp = ensure_ist(timestamp)
        previous_price = self._last_price.get(instrument_token)
        previous_at = self._last_tick_at.get(instrument_token)
        if previous_at is None or previous_at.date() != timestamp.date():
            previous_price = None

        current = self._current.get(instrument_token)
        if current is None:
            self._current[instrument_token] = Candle.from_tick(
                instrument_token, price, timestamp, previous_price
            )
            self._remember_tick(instrument_token, price, timestamp)
            return []
        if start == current.start:
            current.update(price)
            self._remember_tick(instrument_token, price, timestamp)
            return []
        if start < current.start:
            return []

        finalized = self._finalize(instrument_token)
        self._current[instrument_token] = Candle.from_tick(
            instrument_token, price, timestamp, previous_price
        )
        self._remember_tick(instrument_token, price, timestamp)
        return [finalized] if finalized else []

    def _remember_tick(
        self, instrument_token: InstrumentKey, price: Decimal, timestamp: datetime
    ) -> None:
        self._last_price[instrument_token] = price
        self._last_tick_at[instrument_token] = timestamp

    def finalize_due(self, now: datetime, delay_seconds: int = 3) -> list[Candle]:
        now = ensure_ist(now)
        due: list[Candle] = []
        for token, candle in list(self._current.items()):
            if now >= candle.end + timedelta(seconds=delay_seconds):
                finalized = self._finalize(token)
                if finalized:
                    due.append(finalized)
        return due

    def _finalize(self, instrument_token: InstrumentKey) -> Candle | None:
        candle = self._current.pop(instrument_token, None)
        if candle is not None:
            self._last_finalized_end[instrument_token] = candle.end
        return candle
