from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from statistics import mean

from .domain import Candle, calculate_levels

ATR_PERIOD = 14
SWING_WINDOW = 10
SWING_BUFFER_ATR = Decimal("0.10")
DEFAULT_ATR_MULTIPLIER = Decimal("1.50")
ATR_MULTIPLIERS = tuple(
    Decimal(value) for value in ("1.00", "1.25", "1.50", "1.75", "2.00")
)
MIN_RISK_PERCENT = Decimal("0.35")
MAX_RISK_PERCENT = Decimal("3.00")
FALLBACK_VOLATILITY_PERCENT = Decimal("0.60")
ROUND_TRIP_COST_PERCENT = Decimal("0.10")
MIN_BACKTEST_SAMPLES = 8
MIN_VALIDATION_SAMPLES = 3
PRICE_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class StopLossResult:
    trade_side: str
    entry_price: Decimal
    stop_price: Decimal
    risk_amount: Decimal
    risk_percent: Decimal
    method: str
    confidence: str
    backtest_samples: int
    validation_samples: int
    validation_win_rate: Decimal | None
    validation_average_r: Decimal | None
    atr: Decimal
    atr_multiplier: Decimal
    swing_reference: Decimal
    explanation: str


@dataclass(frozen=True)
class _BacktestTrade:
    signal_index: int
    session_indices: tuple[int, ...]


def trade_side(direction: str) -> str:
    """Map the existing retracement alert to its mean-reversion setup side."""
    if direction == "UPPER":
        return "LONG"
    if direction == "LOWER":
        return "SHORT"
    raise ValueError(f"Unsupported alert direction: {direction}")


def _true_range(candle: Candle, previous_close: Decimal | None) -> Decimal:
    values = [candle.high - candle.low]
    if previous_close is not None:
        values.extend(
            (abs(candle.high - previous_close), abs(candle.low - previous_close))
        )
    return max(values)


def _atr(candles: list[Candle], index: int) -> Decimal:
    first = max(0, index - ATR_PERIOD + 1)
    ranges: list[Decimal] = []
    for position in range(first, index + 1):
        previous_close = candles[position - 1].close if position > 0 else None
        ranges.append(_true_range(candles[position], previous_close))
    value = sum(ranges, Decimal("0")) / Decimal(len(ranges)) if ranges else Decimal("0")
    if value > 0:
        return value
    entry = candles[index].close
    return entry * FALLBACK_VOLATILITY_PERCENT / Decimal("100")


def _same_session_window(candles: list[Candle], index: int) -> list[Candle]:
    session_date = candles[index].start.date()
    values = [
        candle
        for candle in candles[: index + 1]
        if candle.start.date() == session_date
    ]
    return values[-SWING_WINDOW:]


def _candidate_stop(
    candles: list[Candle], index: int, side: str, multiplier: Decimal
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    entry = candles[index].close
    atr = _atr(candles, index)
    session_window = _same_session_window(candles, index)
    if side == "LONG":
        swing = min(candle.low for candle in session_window)
        structure_stop = swing - atr * SWING_BUFFER_ATR
        raw_stop = min(entry - atr * multiplier, structure_stop)
        raw_risk = entry - raw_stop
    else:
        swing = max(candle.high for candle in session_window)
        structure_stop = swing + atr * SWING_BUFFER_ATR
        raw_stop = max(entry + atr * multiplier, structure_stop)
        raw_risk = raw_stop - entry

    minimum_risk = entry * MIN_RISK_PERCENT / Decimal("100")
    maximum_risk = entry * MAX_RISK_PERCENT / Decimal("100")
    risk = min(max(raw_risk, minimum_risk), maximum_risk)
    if side == "LONG":
        stop = (entry - risk).quantize(PRICE_QUANTUM, rounding=ROUND_FLOOR)
        risk = entry - stop
    else:
        stop = (entry + risk).quantize(PRICE_QUANTUM, rounding=ROUND_CEILING)
        risk = stop - entry
    return stop, risk, atr, swing


def _historical_trades(
    candles: list[Candle], percentage: Decimal, direction: str, before: date
) -> list[_BacktestTrade]:
    by_session: dict[date, list[int]] = {}
    for index, candle in enumerate(candles):
        session_date = candle.start.date()
        if session_date < before:
            by_session.setdefault(session_date, []).append(index)

    sessions = sorted(by_session)
    trades: list[_BacktestTrade] = []
    for session_position in range(1, len(sessions)):
        session_date = sessions[session_position]
        previous_indices = by_session[sessions[session_position - 1]]
        previous_close = candles[previous_indices[-1]].close
        upper, lower = calculate_levels(previous_close, percentage)
        threshold = upper if direction == "UPPER" else lower
        session_indices = tuple(by_session[session_date])
        for position, index in enumerate(session_indices):
            candle = candles[index]
            crossed = (
                candle.open >= threshold >= candle.low
                if direction == "UPPER"
                else candle.open <= threshold <= candle.high
            )
            if not crossed:
                continue
            future = session_indices[position + 1 :]
            if future:
                trades.append(_BacktestTrade(index, tuple(future)))
            break
    return trades


def _trade_result_r(
    candles: list[Candle], trade: _BacktestTrade, side: str, multiplier: Decimal
) -> Decimal:
    signal = candles[trade.signal_index]
    entry = signal.close
    stop, risk, _, _ = _candidate_stop(candles, trade.signal_index, side, multiplier)
    target = entry + risk if side == "LONG" else entry - risk
    exit_price = candles[trade.session_indices[-1]].close

    for index in trade.session_indices:
        candle = candles[index]
        if side == "LONG":
            # When both levels occur inside one OHLC candle, their order is unknown.
            # Counting the stop first is deliberately conservative.
            if candle.open <= stop:
                exit_price = candle.open
                break
            if candle.open >= target:
                exit_price = target
                break
            if candle.low <= stop:
                exit_price = stop
                break
            if candle.high >= target:
                exit_price = target
                break
        else:
            if candle.open >= stop:
                exit_price = candle.open
                break
            if candle.open <= target:
                exit_price = target
                break
            if candle.high >= stop:
                exit_price = stop
                break
            if candle.low <= target:
                exit_price = target
                break

    gross = exit_price - entry if side == "LONG" else entry - exit_price
    estimated_cost = entry * ROUND_TRIP_COST_PERCENT / Decimal("100")
    return (gross - estimated_cost) / risk


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return Decimal(str(mean(values)))


def _walk_forward_multiplier(
    candles: list[Candle], percentage: Decimal, direction: str, before: date
) -> tuple[Decimal, str, int, int, Decimal | None, Decimal | None, str]:
    side = trade_side(direction)
    trades = _historical_trades(candles, percentage, direction, before)
    total = len(trades)
    if total < MIN_BACKTEST_SAMPLES:
        return (
            DEFAULT_ATR_MULTIPLIER,
            "UNVALIDATED",
            total,
            0,
            None,
            None,
            (
                f"Fixed {DEFAULT_ATR_MULTIPLIER:.2f}× ATR fallback: only {total} "
                "comparable earlier alert(s) were available."
            ),
        )

    split = max(1, int(total * 0.70))
    split = min(split, total - MIN_VALIDATION_SAMPLES)
    training = trades[:split]
    validation = trades[split:]
    training_scores: dict[Decimal, Decimal] = {}
    for multiplier in ATR_MULTIPLIERS:
        training_scores[multiplier] = _average(
            [_trade_result_r(candles, trade, side, multiplier) for trade in training]
        )
    selected = max(
        ATR_MULTIPLIERS,
        key=lambda value: (
            training_scores[value],
            -abs(value - DEFAULT_ATR_MULTIPLIER),
        ),
    )

    validation_results = [
        _trade_result_r(candles, trade, side, selected) for trade in validation
    ]
    validation_average = _average(validation_results)
    validation_win_rate = (
        Decimal(sum(result > 0 for result in validation_results))
        / Decimal(len(validation_results))
        * Decimal("100")
    )

    if validation_average <= 0:
        selected = DEFAULT_ATR_MULTIPLIER
        validation_results = [
            _trade_result_r(candles, trade, side, selected) for trade in validation
        ]
        validation_average = _average(validation_results)
        validation_win_rate = (
            Decimal(sum(result > 0 for result in validation_results))
            / Decimal(len(validation_results))
            * Decimal("100")
        )
        confidence = "LOW"
        explanation = (
            "The fitted stop did not hold up on later historical alerts, so the "
            f"fixed {DEFAULT_ATR_MULTIPLIER:.2f}× ATR fallback is being used."
        )
    else:
        confidence = "HIGH" if (
            len(validation) >= 8
            and validation_average >= Decimal("0.15")
            and validation_win_rate >= Decimal("55")
        ) else "MEDIUM" if (
            len(validation) >= 4 and validation_win_rate >= Decimal("50")
        ) else "LOW"
        explanation = (
            f"Selected {selected:.2f}× ATR on {len(training)} earlier alert(s) and "
            f"checked it on the next {len(validation)} alert(s)."
        )
    return (
        selected,
        confidence,
        total,
        len(validation),
        validation_win_rate,
        validation_average,
        explanation,
    )


def calculate_stop_loss(
    *,
    history: list[Candle],
    alert_candle: Candle,
    percentage: Decimal,
    direction: str,
) -> StopLossResult:
    """Calculate a protective stop using only information known by alert close.

    Historical matching alerts are ordered chronologically. The first 70% select
    one of five fixed ATR multipliers and the remaining 30% validate it. Sparse or
    unsuccessful validation falls back to 1.5× ATR instead of claiming an optimum.
    """
    known = [candle for candle in history if candle.end <= alert_candle.end]
    if not any(
        candle.start == alert_candle.start
        and candle.instrument_token == alert_candle.instrument_token
        for candle in known
    ):
        known.append(alert_candle)
    known.sort(key=lambda candle: candle.start)
    alert_index = max(
        index
        for index, candle in enumerate(known)
        if candle.start == alert_candle.start
        and candle.instrument_token == alert_candle.instrument_token
    )

    side = trade_side(direction)
    (
        multiplier,
        confidence,
        samples,
        validation_samples,
        win_rate,
        average_r,
        explanation,
    ) = _walk_forward_multiplier(
        known, percentage, direction, alert_candle.start.date()
    )
    stop, risk, atr, swing = _candidate_stop(known, alert_index, side, multiplier)
    risk_percent = risk / alert_candle.close * Decimal("100")
    method = (
        "Walk-forward ATR + swing"
        if explanation.startswith("Selected ")
        else "ATR + swing fallback"
    )
    return StopLossResult(
        trade_side=side,
        entry_price=alert_candle.close,
        stop_price=stop,
        risk_amount=risk,
        risk_percent=risk_percent,
        method=method,
        confidence=confidence,
        backtest_samples=samples,
        validation_samples=validation_samples,
        validation_win_rate=win_rate,
        validation_average_r=average_r,
        atr=atr,
        atr_multiplier=multiplier,
        swing_reference=swing,
        explanation=explanation,
    )


def fallback_stop_loss(
    *, alert_candle: Candle, direction: str, reason: str | None = None
) -> StopLossResult:
    """Return a deterministic stop when stored calibration history is unusable."""
    side = trade_side(direction)
    stop, risk, atr, swing = _candidate_stop(
        [alert_candle], 0, side, DEFAULT_ATR_MULTIPLIER
    )
    explanation = (
        "Historical calibration was unavailable, so the fixed 1.50× ATR "
        "current-candle fallback was used."
    )
    if reason:
        explanation += f" Reason: {reason[:160]}"
    return StopLossResult(
        trade_side=side,
        entry_price=alert_candle.close,
        stop_price=stop,
        risk_amount=risk,
        risk_percent=risk / alert_candle.close * Decimal(100),
        method="Current-candle safety fallback",
        confidence="UNVALIDATED",
        backtest_samples=0,
        validation_samples=0,
        validation_win_rate=None,
        validation_average_r=None,
        atr=atr,
        atr_multiplier=DEFAULT_ATR_MULTIPLIER,
        swing_reference=swing,
        explanation=explanation,
    )
