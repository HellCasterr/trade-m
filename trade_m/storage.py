from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .domain import Candle, display_price


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
                    created_at TEXT NOT NULL,
                    UNIQUE(rule_id, direction, candle_start),
                    FOREIGN KEY(rule_id) REFERENCES rules(id)
                );
                """
            )

    def upsert_rule(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        instrument_token: int,
        trading_date: date,
        percentage: Decimal,
        reference_date: date,
        reference_close: Decimal,
        upper_level: Decimal,
        lower_level: Decimal,
    ) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self.connection() as connection:
            connection.execute(
                """
                INSERT INTO rules (
                    exchange, tradingsymbol, instrument_token, trading_date,
                    percentage, reference_date, reference_close, upper_level,
                    lower_level, upper_sent, lower_sent, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?)
                ON CONFLICT(exchange, tradingsymbol, trading_date) DO UPDATE SET
                    instrument_token = excluded.instrument_token,
                    percentage = excluded.percentage,
                    reference_date = excluded.reference_date,
                    reference_close = excluded.reference_close,
                    upper_level = excluded.upper_level,
                    lower_level = excluded.lower_level,
                    upper_sent = 0,
                    lower_sent = 0,
                    active = 1,
                    created_at = excluded.created_at
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
                ),
            )
            row = connection.execute(
                "SELECT id FROM rules WHERE exchange=? AND tradingsymbol=? AND trading_date=?",
                (exchange, tradingsymbol, trading_date.isoformat()),
            ).fetchone()
            return int(row["id"])

    def active_rules(self, trading_date: date) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rules WHERE trading_date=? AND active=1 ORDER BY exchange, tradingsymbol",
                (trading_date.isoformat(),),
            ).fetchall()
        return [self._rule_dict(row) for row in rows]

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return self._rule_dict(row) if row else None

    def rules_for_token(self, trading_date: date, token: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rules WHERE trading_date=? AND instrument_token=? AND active=1",
                (trading_date.isoformat(), token),
            ).fetchall()
        return [self._rule_dict(row) for row in rows]

    def deactivate_rule(self, rule_id: int) -> bool:
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                "UPDATE rules SET active=0 WHERE id=?", (rule_id,)
            )
            return cursor.rowcount > 0

    def evaluate_candle(self, candle: Candle) -> list[dict[str, Any]]:
        trading_date = candle.start.date()
        created: list[dict[str, Any]] = []
        with self._lock, self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rules WHERE trading_date=? AND instrument_token=? AND active=1",
                (trading_date.isoformat(), candle.instrument_token),
            ).fetchall()
            for row in rows:
                candidates = (
                    ("UPPER", Decimal(row["upper_level"]), bool(row["upper_sent"])),
                    ("LOWER", Decimal(row["lower_level"]), bool(row["lower_sent"])),
                )
                for direction, threshold, already_sent in candidates:
                    if already_sent or not candle.contains(threshold):
                        continue
                    now = datetime.now(UTC).isoformat(timespec="seconds")
                    try:
                        cursor = connection.execute(
                            """
                            INSERT INTO events (
                                rule_id, direction, candle_start, candle_end,
                                candle_open, candle_high, candle_low, candle_close,
                                threshold, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            "created_at": now,
                        }
                    )
        return created

    def events_after(self, event_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.*, r.exchange, r.tradingsymbol, r.percentage, r.reference_close
                FROM events e JOIN rules r ON r.id=e.rule_id
                WHERE e.id>? ORDER BY e.id ASC LIMIT ?
                """,
                (event_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["threshold_display"] = display_price(Decimal(item["threshold"]))
            result.append(item)
        return result

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
