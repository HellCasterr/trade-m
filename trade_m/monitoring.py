from __future__ import annotations

import threading
import time as time_module
from collections import defaultdict
from collections.abc import Hashable
from datetime import datetime, timedelta
from typing import Any

from .domain import IST, Candle, bucket_start, ensure_ist, session_bounds


class ReliableMonitorMixin:
    """Shared recovery and subscription reliability for live provider monitors."""

    subscription_reconcile_seconds = 10
    recovery_retry_seconds = 15

    def _init_reliability(self) -> None:
        self._desired_tokens: set[Hashable] = set()
        self._subscribed_tokens: set[Hashable] = set()
        self._recovery_requests: dict[Hashable, tuple[datetime, float]] = {}
        self._recovering_tokens: set[Hashable] = set()
        self._gapped_tokens: set[Hashable] = set()
        self._pending_candles: dict[Hashable, list[Candle]] = defaultdict(list)
        self._recovery_event = threading.Event()
        self._subscription_event = threading.Event()
        self._recovery_thread: threading.Thread | None = None
        self._reconciliation_thread: threading.Thread | None = None

    def _start_reliability_workers(self, generation: int) -> None:
        with self._lock:
            self._subscribed_tokens.clear()
            self._recovery_requests.clear()
            self._recovering_tokens.clear()
            self._gapped_tokens.clear()
            self._pending_candles.clear()
        self._recovery_event.clear()
        self._subscription_event.clear()
        self._recovery_thread = threading.Thread(
            target=self._recovery_loop,
            args=(generation,),
            name=f"trade-m-{self.provider}-recovery",
            daemon=True,
        )
        self._reconciliation_thread = threading.Thread(
            target=self._reconciliation_loop,
            args=(generation,),
            name=f"trade-m-{self.provider}-subscription-reconciliation",
            daemon=True,
        )
        self._recovery_thread.start()
        self._reconciliation_thread.start()

    def _wake_reliability_workers(self) -> None:
        self._recovery_event.set()
        self._subscription_event.set()

    def _coerce_token(self, token: int | str) -> Hashable:
        return token

    def _active_rule_tokens(self) -> set[Hashable]:
        today = datetime.now(IST).date()
        return {
            self._coerce_token(rule["instrument_token"])
            for rule in self.store.active_rules(today, provider=self.provider)
        }

    def _request_subscriptions(self, tokens: list[int | str]) -> None:
        requested = {self._coerce_token(token) for token in tokens}
        if not requested:
            return
        with self._lock:
            self._desired_tokens.update(requested)
            connected = self.connected
        if connected:
            subscribed = self._subscribe_batches(requested)
            with self._lock:
                self._subscribed_tokens.update(subscribed)
            self._queue_recovery(subscribed)
        self._subscription_event.set()

    def _connection_ready(self) -> None:
        with self._lock:
            self._subscribed_tokens.clear()
        self._reconcile_subscriptions()

    def _connection_lost(self) -> None:
        with self._lock:
            self._subscribed_tokens.clear()
        self._subscription_event.set()

    def _reconciliation_loop(self, generation: int) -> None:
        while self.running and self._generation == generation:
            try:
                self._reconcile_subscriptions()
            except Exception as exc:
                self._record_error(
                    f"{self.provider.title()} subscription reconciliation failed: {exc}"
                )
            self._subscription_event.wait(self.subscription_reconcile_seconds)
            self._subscription_event.clear()

    def _reconcile_subscriptions(self) -> None:
        desired = self._active_rule_tokens()
        with self._lock:
            self._desired_tokens = desired
            self._subscribed_tokens.intersection_update(desired)
            if not self.connected:
                return
            missing = desired - self._subscribed_tokens
        if not missing:
            self._queue_recovery(desired)
            return
        subscribed = self._subscribe_batches(missing)
        with self._lock:
            self._subscribed_tokens.update(subscribed)
        self._queue_recovery(subscribed)

    def _subscribe_batches(self, tokens: set[Hashable]) -> set[Hashable]:
        raise NotImplementedError

    def _subscription_status(self) -> dict[str, int]:
        desired = self._active_rule_tokens()
        with self._lock:
            subscribed = self._subscribed_tokens & desired
        return {
            "desired_subscriptions": len(desired),
            "subscribed_instruments": len(subscribed),
            "subscription_gap": len(desired - subscribed),
        }

    def _completed_through(self, now: datetime | None = None) -> datetime | None:
        now = ensure_ist(now or datetime.now(IST))
        opening, closing = session_bounds(now.date())
        if now <= opening:
            return None
        if now >= closing + timedelta(seconds=self.finalization_delay_seconds):
            return closing
        safe_now = now - timedelta(seconds=self.finalization_delay_seconds)
        return bucket_start(safe_now)

    def _queue_recovery(self, tokens: set[Hashable] | list[Hashable]) -> None:
        if self.gateway is None:
            return
        now = datetime.now(IST)
        completed_through = self._completed_through(now)
        if completed_through is None:
            return
        opening, _ = session_bounds(now.date())
        queued = False
        with self._lock:
            for raw_token in tokens:
                token = self._coerce_token(raw_token)
                if token in self._recovering_tokens:
                    self._gapped_tokens.add(token)
                    continue
                checkpoint = self.store.candle_checkpoint(
                    self.provider, token, now.date()
                )
                since = checkpoint or opening
                if since >= completed_through:
                    continue
                current = self._recovery_requests.get(token)
                if current is None or since < current[0]:
                    self._recovery_requests[token] = (since, 0.0)
                self._gapped_tokens.add(token)
                queued = True
        if queued:
            self._recovery_event.set()

    def _next_recovery_request(
        self, generation: int
    ) -> tuple[Hashable, datetime] | None:
        if not self.running or self._generation != generation:
            return None
        now_monotonic = time_module.monotonic()
        with self._lock:
            due = [
                (token, value)
                for token, value in self._recovery_requests.items()
                if value[1] <= now_monotonic and token not in self._recovering_tokens
            ]
            if not due:
                return None
            token, (since, _) = min(due, key=lambda item: item[1][1])
            self._recovery_requests.pop(token, None)
            self._recovering_tokens.add(token)
            return token, since

    def _recovery_loop(self, generation: int) -> None:
        while self.running and self._generation == generation:
            request = self._next_recovery_request(generation)
            if request is None:
                self._recovery_event.wait(1)
                self._recovery_event.clear()
                continue
            token, since = request
            until = self._completed_through()
            if until is None or since >= until:
                self._finish_recovery(token, success=True, completed_through=until)
                continue
            try:
                candles = self.gateway.completed_candles(token, since, until)
                if not self.running or self._generation != generation:
                    return
                persisted = self._process_direct(
                    sorted(candles, key=lambda candle: candle.end),
                    update_checkpoint=True,
                )
                if not persisted:
                    raise RuntimeError("one or more recovered candles could not be persisted")
                self._finish_recovery(
                    token, success=True, completed_through=until
                )
            except Exception as exc:
                if not self.running or self._generation != generation:
                    return
                self._record_error(
                    f"{self.provider.title()} recovery failed for {token}: {exc}"
                )
                self._finish_recovery(
                    token,
                    success=False,
                    completed_through=None,
                    retry_since=since,
                )

    def _finish_recovery(
        self,
        token: Hashable,
        *,
        success: bool,
        completed_through: datetime | None,
        retry_since: datetime | None = None,
    ) -> None:
        if success and completed_through is not None:
            self.store.advance_candle_checkpoint(
                self.provider, token, completed_through.date(), completed_through
            )

        while True:
            with self._lock:
                pending = self._pending_candles.pop(token, [])
                if not pending:
                    self._recovering_tokens.discard(token)
                    if success:
                        self._gapped_tokens.discard(token)
                    elif retry_since is not None:
                        self._recovery_requests[token] = (
                            retry_since,
                            time_module.monotonic() + self.recovery_retry_seconds,
                        )
                    break
            self._process_direct(
                sorted(pending, key=lambda candle: candle.end),
                update_checkpoint=success,
            )
        if not success:
            self._recovery_event.set()

    def _process_reliably(self, candles: list[Candle]) -> None:
        immediate: list[tuple[Candle, bool]] = []
        with self._lock:
            for candle in candles:
                try:
                    token = self._coerce_token(candle.instrument_token)
                except Exception as exc:
                    self._record_error(
                        f"{self.provider.title()} candle processing failed: {exc}"
                    )
                    continue
                if token in self._recovering_tokens:
                    self._pending_candles[token].append(candle)
                else:
                    immediate.append((candle, token not in self._gapped_tokens))
        for candle, update_checkpoint in immediate:
            self._process_direct([candle], update_checkpoint=update_checkpoint)

    def _process_direct(
        self, candles: list[Candle], *, update_checkpoint: bool
    ) -> bool:
        succeeded = True
        for candle in candles:
            try:
                events = self.store.evaluate_candle(
                    candle,
                    provider=self.provider,
                    update_checkpoint=update_checkpoint,
                )
                with self._lock:
                    self.processed_candles += 1
                for event in events:
                    try:
                        self.on_event(event)
                    except Exception as exc:
                        self._record_error(
                            f"{self.provider.title()} event callback failed: {exc}"
                        )
            except Exception as exc:
                succeeded = False
                self._record_error(
                    f"{self.provider.title()} candle processing failed: {exc}"
                )
        return succeeded

    def _reliability_status(self) -> dict[str, Any]:
        with self._lock:
            recovering = len(self._recovering_tokens)
            recovery_queue = len(self._recovery_requests)
            recovery_gaps = len(self._gapped_tokens)
        return {
            **self._subscription_status(),
            "recovering": bool(recovering or recovery_queue),
            "recovering_instruments": recovering,
            "recovery_queue": recovery_queue,
            "recovery_gaps": recovery_gaps,
        }
