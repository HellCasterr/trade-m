from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .domain import Candle, calculate_levels, display_price, ensure_ist
from .stop_loss import calculate_stop_loss, fallback_stop_loss

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._lock, self.connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL,
                    tradingsymbol TEXT NOT NULL,
                    instrument_token INTEGER NOT NULL,
                    trading_date TEXT NOT NULL,
                    percentage TEXT NOT NULL,
                    reference_date TEXT NOT NULL,
                    reference_close TEXT NOT NULL,
                    upper_level TEXT NOT NULL,
                    lower_level TEXT NOT NULL,
                    upper_sent INTEGER NOT NULL DEFAULT 0,
                    lower_sent INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(exchange, tradingsymbol, trading_date)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    candle_start TEXT NOT NULL,
                    candle_end TEXT NOT NULL,
                    candle_open TEXT NOT NULL,
                    candle_high TEXT NOT NULL,
                    candle_low TEXT NOT NULL,
                    candle_close TEXT NOT NULL,
                    threshold TEXT NOT NULL,
                    trade_side TEXT,
                    entry_price TEXT,
                    stop_loss TEXT,
                    risk_amount TEXT,
                    risk_percent TEXT,
                    stop_method TEXT,
                    stop_confidence TEXT,
                    backtest_samples INTEGER,
                    validation_samples INTEGER,
                    validation_win_rate TEXT,
                    validation_average_r TEXT,
                    atr TEXT,
                    atr_multiplier TEXT,
                    swing_reference TEXT,
                    stop_explanation TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(rule_id, direction, candle_start),
                    FOREIGN KEY(rule_id) REFERENCES rules(id)
                );
                CREATE TABLE IF NOT EXISTS candle_checkpoints (
                    provider TEXT NOT NULL,
                    instrument_token TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    last_candle_end TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, instrument_token, trading_date)
                );
                CREATE TABLE IF NOT EXISTS market_candles (
                    provider TEXT NOT NULL,
                    instrument_token TEXT NOT NULL,
                    candle_start TEXT NOT NULL,
                    candle_end TEXT NOT NULL,
                    candle_open TEXT NOT NULL,
                    candle_high TEXT NOT NULL,
                    candle_low TEXT NOT NULL,
                    candle_close TEXT NOT NULL,
                    PRIMARY KEY(provider, instrument_token, candle_start)
                );
                CREATE INDEX IF NOT EXISTS idx_market_candles_lookup
                    ON market_candles(provider, instrument_token, candle_end);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(rules)").fetchall()
            }
            if "provider" not in columns:
                connection.execute(
                    "ALTER TABLE rules ADD COLUMN provider TEXT NOT NULL DEFAULT 'zerodha'"
                )
            event_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            additions = {
                "trade_side": "TEXT",
                "entry_price": "TEXT",
                "stop_loss": "TEXT",
                "risk_amount": "TEXT",
                "risk_percent": "TEXT",
                "stop_method": "TEXT",
                "stop_confidence": "TEXT",
                "backtest_samples": "INTEGER",
                "validation_samples": "INTEGER",
                "validation_win_rate": "TEXT",
                "validation_average_r": "TEXT",
                "atr": "TEXT",
                "atr_multiplier": "TEXT",
                "swing_reference": "TEXT",
                "stop_explanation": "TEXT",
            }
            for name, data_type in additions.items():
                if name not in event_columns:
                    connection.execute(
                        f"ALTER TABLE events ADD COLUMN {name} {data_type}"
                    )

    def upsert_rule(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        instrument_token: int | str,
        trading_date: date,
        percentage: Decimal,
        reference_date: date,
        reference_close: Decimal,
        upper_level: Decimal,
        lower_level: Decimal,
        provider: str = "zerodha",
    ) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self.connection() as connection:
            connection.execute(
                """
                INSERT INTO rules (
                    exchange, tradingsymbol, instrument_token, trading_date,
                    percentage, reference_date, reference_close, upper_level,
                    lower_level, upper_sent, lower_sent, active, created_at, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?)
                ON CONFLICT(exchange, tradingsymbol, trading_date) DO UPDATE SET
                    instrument_token = excluded.instrument_token,
                    percentage = excluded.percentage,
                    reference_date = excluded.reference_date,
                    reference_close = excluded.reference_close,
                    upper_level = excluded.upper_level,
                    lower_level = excluded.lower_level,
                    upper_sent = CASE
                        WHEN rules.provider = excluded.provider
                         AND CAST(rules.instrument_token AS TEXT) = CAST(excluded.instrument_token AS TEXT)
                         AND CAST(rules.percentage AS NUMERIC) = CAST(excluded.percentage AS NUMERIC)
                         AND rules.reference_date = excluded.reference_date
                         AND CAST(rules.reference_close AS NUMERIC) = CAST(excluded.reference_close AS NUMERIC)
                         AND CAST(rules.upper_level AS NUMERIC) = CAST(excluded.upper_level AS NUMERIC)
                         AND CAST(rules.lower_level AS NUMERIC) = CAST(excluded.lower_level AS NUMERIC)
                        THEN rules.upper_sent ELSE 0 END,
                    lower_sent = CASE
                        WHEN rules.provider = excluded.provider
                         AND CAST(rules.instrument_token AS TEXT) = CAST(excluded.instrument_token AS TEXT)
                         AND CAST(rules.percentage AS NUMERIC) = CAST(excluded.percentage AS NUMERIC)
                         AND rules.reference_date = excluded.reference_date
                         AND CAST(rules.reference_close AS NUMERIC) = CAST(excluded.reference_close AS NUMERIC)
                         AND CAST(rules.upper_level AS NUMERIC) = CAST(excluded.upper_level AS NUMERIC)
                         AND CAST(rules.lower_level AS NUMERIC) = CAST(excluded.lower_level AS NUMERIC)
                        THEN rules.lower_sent ELSE 0 END,
                    active = 1,
                    created_at = excluded.created_at,
                    provider = excluded.provider
                """,
                (
                    exchange,
                    tradingsymbol,
                    instrument_token,
                    trading_date.isoformat(),
                    str(percentage),
                    reference_date.isoformat(),
                    str(reference_close),
                    str(upper_level),
                    str(lower_level),
                    now,
                    provider,
                ),
            )
            row = connection.execute(
                "SELECT id FROM rules WHERE exchange=? AND tradingsymbol=? AND trading_date=?",
                (exchange, tradingsymbol, trading_date.isoformat()),
            ).fetchone()
            return int(row["id"])

    def daily_rules(self, trading_date: date) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rules WHERE trading_date=? ORDER BY exchange, tradingsymbol",
                (trading_date.isoformat(),),
            ).fetchall()
        return [self._rule_dict(row) for row in rows]

    def active_rules(
        self, trading_date: date, provider: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM rules WHERE trading_date=? AND active=1"
        params: list[Any] = [trading_date.isoformat()]
        if provider is not None:
            query += " AND provider=?"
            params.append(provider)
        query += " ORDER BY exchange, tradingsymbol"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._rule_dict(row) for row in rows]

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return self._rule_dict(row) if row else None

    def rules_for_token(
        self, trading_date: date, token: int | str, provider: str = "zerodha"
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM rules WHERE trading_date=? AND instrument_token=?
                   AND provider=? AND active=1""",
                (trading_date.isoformat(), token, provider),
            ).fetchall()
        return [self._rule_dict(row) for row in rows]

    def deactivate_rule(self, rule_id: int) -> bool:
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                "UPDATE rules SET active=0 WHERE id=?", (rule_id,)
            )
            return cursor.rowcount > 0

    def set_rule_active(self, rule_id: int, active: bool) -> bool:
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                "UPDATE rules SET active=? WHERE id=?", (int(active), rule_id)
            )
            return cursor.rowcount > 0

    def set_rules_active(
        self, trading_date: date, active: bool, rule_ids: list[int] | None = None
    ) -> int:
        query = "UPDATE rules SET active=? WHERE trading_date=?"
        params: list[Any] = [int(active), trading_date.isoformat()]
        if rule_ids is not None:
            if not rule_ids:
                return 0
            placeholders = ",".join("?" for _ in rule_ids)
            query += f" AND id IN ({placeholders})"
            params.extend(rule_ids)
        with self._lock, self.connection() as connection:
            cursor = connection.execute(query, params)
            return cursor.rowcount

    def update_rule_percentage(
        self, rule_id: int, percentage: Decimal
    ) -> dict[str, Any] | None:
        with self._lock, self.connection() as connection:
            row = connection.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            if row is None:
                return None
            upper, lower = calculate_levels(Decimal(row["reference_close"]), percentage)
            connection.execute(
                """UPDATE rules SET percentage=?, upper_level=?, lower_level=?,
                   upper_sent=0, lower_sent=0 WHERE id=?""",
                (str(percentage), str(upper), str(lower), rule_id),
            )
        return self.get_rule(rule_id)

    def save_market_candles(
        self, provider: str, candles: list[Candle]
    ) -> int:
        """Persist broker history so stop calculations never wait on an alert."""
        if not candles:
            return 0
        with self._lock, self.connection() as connection:
            for candle in candles:
                self._save_market_candle(connection, provider, candle)
        return len(candles)

    @staticmethod
    def _save_market_candle(
        connection: sqlite3.Connection, provider: str, candle: Candle
    ) -> None:
        connection.execute(
            """
            INSERT INTO market_candles (
                provider, instrument_token, candle_start, candle_end,
                candle_open, candle_high, candle_low, candle_close
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, instrument_token, candle_start) DO UPDATE SET
                candle_end=excluded.candle_end,
                candle_open=excluded.candle_open,
                candle_high=excluded.candle_high,
                candle_low=excluded.candle_low,
                candle_close=excluded.candle_close
            """,
            (
                provider,
                str(candle.instrument_token),
                ensure_ist(candle.start).isoformat(),
                ensure_ist(candle.end).isoformat(),
                str(candle.open),
                str(candle.high),
                str(candle.low),
                str(candle.close),
            ),
        )

    @staticmethod
    def _market_candles_through(
        connection: sqlite3.Connection,
        provider: str,
        instrument_token: int | str,
        candle_end: datetime,
    ) -> list[Candle]:
        rows = connection.execute(
            """
            SELECT * FROM market_candles
            WHERE provider=? AND instrument_token=?
              AND candle_end>=? AND candle_end<=?
            ORDER BY candle_start ASC
            """,
            (
                provider,
                str(instrument_token),
                (ensure_ist(candle_end) - timedelta(days=28)).isoformat(),
                ensure_ist(candle_end).isoformat(),
            ),
        ).fetchall()
        return [
            Candle.from_ohlc(
                instrument_token=instrument_token,
                start=datetime.fromisoformat(row["candle_start"]),
                end=datetime.fromisoformat(row["candle_end"]),
                open=Decimal(row["candle_open"]),
                high=Decimal(row["candle_high"]),
                low=Decimal(row["candle_low"]),
                close=Decimal(row["candle_close"]),
            )
            for row in rows
        ]

    def candle_checkpoint(
        self, provider: str, instrument_token: int | str, trading_date: date
    ) -> datetime | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT last_candle_end FROM candle_checkpoints
                   WHERE provider=? AND instrument_token=? AND trading_date=?""",
                (provider, str(instrument_token), trading_date.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return ensure_ist(datetime.fromisoformat(row["last_candle_end"]))

    def advance_candle_checkpoint(
        self,
        provider: str,
        instrument_token: int | str,
        trading_date: date,
        candle_end: datetime,
    ) -> None:
        with self._lock, self.connection() as connection:
            self._advance_candle_checkpoint(
                connection, provider, instrument_token, trading_date, candle_end
            )

    @staticmethod
    def _advance_candle_checkpoint(
        connection: sqlite3.Connection,
        provider: str,
        instrument_token: int | str,
        trading_date: date,
        candle_end: datetime,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO candle_checkpoints (
                provider, instrument_token, trading_date, last_candle_end, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, instrument_token, trading_date) DO UPDATE SET
                last_candle_end = excluded.last_candle_end,
                updated_at = excluded.updated_at
            WHERE excluded.last_candle_end > candle_checkpoints.last_candle_end
            """,
            (
                provider,
                str(instrument_token),
                trading_date.isoformat(),
                ensure_ist(candle_end).isoformat(),
                now,
            ),
        )

    def evaluate_candle(
        self,
        candle: Candle,
        provider: str = "zerodha",
        *,
        update_checkpoint: bool = True,
    ) -> list[dict[str, Any]]:
        trading_date = candle.start.date()
        created: list[dict[str, Any]] = []
        with self._lock, self.connection() as connection:
            if update_checkpoint:
                checkpoint = connection.execute(
                    """SELECT last_candle_end FROM candle_checkpoints
                       WHERE provider=? AND instrument_token=? AND trading_date=?""",
                    (provider, str(candle.instrument_token), trading_date.isoformat()),
                ).fetchone()
                if checkpoint is not None:
                    last_end = ensure_ist(
                        datetime.fromisoformat(checkpoint["last_candle_end"])
                    )
                    if candle.end <= last_end:
                        return []
            self._save_market_candle(connection, provider, candle)
            rows = connection.execute(
                """SELECT * FROM rules WHERE trading_date=? AND instrument_token=?
                   AND provider=? AND active=1""",
                (trading_date.isoformat(), candle.instrument_token, provider),
            ).fetchall()
            for row in rows:
                candidates = (
                    ("UPPER", Decimal(row["upper_level"]), bool(row["upper_sent"])),
                    ("LOWER", Decimal(row["lower_level"]), bool(row["lower_sent"])),
                )
                for direction, threshold, already_sent in candidates:
                    crossed = (
                        candle.crossed_down(threshold)
                        if direction == "UPPER"
                        else candle.crossed_up(threshold)
                    )
                    if already_sent or not crossed:
                        continue
                    try:
                        history = self._market_candles_through(
                            connection, provider, candle.instrument_token, candle.end
                        )
                        stop = calculate_stop_loss(
                            history=history,
                            alert_candle=candle,
                            percentage=Decimal(row["percentage"]),
                            direction=direction,
                        )
                    except Exception as exc:
                        logger.exception(
                            "Stop calibration failed for %s rule %s",
                            provider,
                            row["id"],
                        )
                        stop = fallback_stop_loss(
                            alert_candle=candle,
                            direction=direction,
                        )
                    now = datetime.now(UTC).isoformat(timespec="seconds")
                    try:
                        cursor = connection.execute(
                            """
                            INSERT INTO events (
                                rule_id, direction, candle_start, candle_end,
                                candle_open, candle_high, candle_low, candle_close,
                                threshold, trade_side, entry_price, stop_loss,
                                risk_amount, risk_percent, stop_method,
                                stop_confidence, backtest_samples,
                                validation_samples, validation_win_rate,
                                validation_average_r, atr, atr_multiplier,
                                swing_reference, stop_explanation, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["id"],
                                direction,
                                candle.start.isoformat(),
                                candle.end.isoformat(),
                                str(candle.open),
                                str(candle.high),
                                str(candle.low),
                                str(candle.close),
                                str(threshold),
                                stop.trade_side,
                                str(stop.entry_price),
                                str(stop.stop_price),
                                str(stop.risk_amount),
                                str(stop.risk_percent),
                                stop.method,
                                stop.confidence,
                                stop.backtest_samples,
                                stop.validation_samples,
                                (
                                    str(stop.validation_win_rate)
                                    if stop.validation_win_rate is not None
                                    else None
                                ),
                                (
                                    str(stop.validation_average_r)
                                    if stop.validation_average_r is not None
                                    else None
                                ),
                                str(stop.atr),
                                str(stop.atr_multiplier),
                                str(stop.swing_reference),
                                stop.explanation,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    sent_column = "upper_sent" if direction == "UPPER" else "lower_sent"
                    connection.execute(
                        f"UPDATE rules SET {sent_column}=1 WHERE id=?", (row["id"],)
                    )
                    created.append(
                        {
                            "id": int(cursor.lastrowid),
                            "rule_id": int(row["id"]),
                            "direction": direction,
                            "exchange": row["exchange"],
                            "tradingsymbol": row["tradingsymbol"],
                            "provider": row["provider"],
                            "percentage": row["percentage"],
                            "reference_close": row["reference_close"],
                            "threshold": str(threshold),
                            "threshold_display": display_price(threshold),
                            "candle_start": candle.start.isoformat(),
                            "candle_end": candle.end.isoformat(),
                            "candle_open": str(candle.open),
                            "candle_high": str(candle.high),
                            "candle_low": str(candle.low),
                            "candle_close": str(candle.close),
                            "trade_side": stop.trade_side,
                            "entry_price": str(stop.entry_price),
                            "entry_price_display": display_price(stop.entry_price),
                            "stop_loss": str(stop.stop_price),
                            "stop_loss_display": display_price(stop.stop_price),
                            "risk_amount": str(stop.risk_amount),
                            "risk_percent": str(stop.risk_percent),
                            "risk_percent_display": self._display_percent(
                                stop.risk_percent
                            ),
                            "stop_method": stop.method,
                            "stop_confidence": stop.confidence,
                            "backtest_samples": stop.backtest_samples,
                            "validation_samples": stop.validation_samples,
                            "validation_win_rate": (
                                str(stop.validation_win_rate)
                                if stop.validation_win_rate is not None
                                else None
                            ),
                            "validation_win_rate_display": (
                                self._display_percent(stop.validation_win_rate)
                                if stop.validation_win_rate is not None
                                else None
                            ),
                            "validation_average_r": (
                                str(stop.validation_average_r)
                                if stop.validation_average_r is not None
                                else None
                            ),
                            "atr": str(stop.atr),
                            "atr_multiplier": str(stop.atr_multiplier),
                            "swing_reference": str(stop.swing_reference),
                            "stop_explanation": stop.explanation,
                            "created_at": now,
                        }
                    )
            if update_checkpoint:
                self._advance_candle_checkpoint(
                    connection,
                    provider,
                    candle.instrument_token,
                    trading_date,
                    candle.end,
                )
        return created

    def latest_event_id(self) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM events"
            ).fetchone()
        return int(row["id"] if row else 0)

    def events_after(
        self,
        event_id: int = 0,
        limit: int = 100,
        trading_date: date | None = None,
    ) -> list[dict[str, Any]]:
        where = "WHERE e.id>?"
        params: list[Any] = [event_id]
        if trading_date is not None:
            where += " AND r.trading_date=?"
            params.append(trading_date.isoformat())
        params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT e.*, r.exchange, r.tradingsymbol, r.percentage,
                       r.reference_close, r.provider
                FROM events e JOIN rules r ON r.id=e.rule_id
                {where} ORDER BY e.id ASC LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["threshold_display"] = display_price(Decimal(item["threshold"]))
            if item.get("entry_price") is not None:
                item["entry_price_display"] = display_price(
                    Decimal(item["entry_price"])
                )
            if item.get("stop_loss") is not None:
                item["stop_loss_display"] = display_price(Decimal(item["stop_loss"]))
            if item.get("risk_percent") is not None:
                item["risk_percent_display"] = self._display_percent(
                    Decimal(item["risk_percent"])
                )
            if item.get("validation_win_rate") is not None:
                item["validation_win_rate_display"] = self._display_percent(
                    Decimal(item["validation_win_rate"])
                )
            result.append(item)
        return result

    @staticmethod
    def _display_percent(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))}%"

    @staticmethod
    def _rule_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["active"] = bool(item["active"])
        item["upper_sent"] = bool(item["upper_sent"])
        item["lower_sent"] = bool(item["lower_sent"])
        item["reference_close_display"] = display_price(Decimal(item["reference_close"]))
        item["upper_level_display"] = display_price(Decimal(item["upper_level"]))
        item["lower_level_display"] = display_price(Decimal(item["lower_level"]))
        return item
