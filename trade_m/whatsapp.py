from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

import requests

from .domain import IST

logger = logging.getLogger(__name__)

_RETRYABLE_META_CODES = {4, 80007, 130429, 131048, 131056}


class WhatsAppError(RuntimeError):
    """Raised when Meta's WhatsApp Cloud API does not accept an alert."""


def normalise_whatsapp_number(value: str) -> str:
    """Return a WhatsApp recipient in digits-only international format."""
    digits = re.sub(r"\D", "", value or "")
    if not 8 <= len(digits) <= 15:
        raise ValueError(
            "WHATSAPP_TO must contain an international phone number with country code."
        )
    return digits


class WhatsAppNotifier:
    """Send Trade M alert events through Meta's WhatsApp Cloud API.

    Trade M only needs outbound HTTPS access to send messages. A public webhook is
    optional and is not required by this sender.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        access_token: str,
        phone_number_id: str,
        recipient: str,
        template_name: str = "trade_m_alert_v1",
        template_language: str = "en_US",
        graph_version: str = "v26.0",
        test_template_name: str = "hello_world",
        timeout_seconds: float = 15.0,
        session: requests.Session | Any | None = None,
    ) -> None:
        self.enabled = enabled
        self.access_token = access_token.strip()
        self.phone_number_id = phone_number_id.strip()
        self.recipient_raw = recipient.strip()
        self.template_name = template_name.strip() or "trade_m_alert_v1"
        self.template_language = template_language.strip() or "en_US"
        self.graph_version = graph_version.strip().lstrip("/") or "v26.0"
        if not self.graph_version.startswith("v"):
            self.graph_version = f"v{self.graph_version}"
        self.test_template_name = test_template_name.strip() or "hello_world"
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.last_message_id: str | None = None
        self.last_sent_at: datetime | None = None

    @property
    def credentials_present(self) -> bool:
        return bool(
            self.access_token
            and self.phone_number_id
            and self.recipient_raw
            and self.template_name
        )

    @property
    def configured(self) -> bool:
        return self.enabled and self.credentials_present

    @property
    def recipient(self) -> str:
        return normalise_whatsapp_number(self.recipient_raw)

    @property
    def endpoint(self) -> str:
        return (
            f"https://graph.facebook.com/{self.graph_version}/"
            f"{self.phone_number_id}/messages"
        )

    def status(self) -> dict[str, Any]:
        recipient_hint = None
        if self.recipient_raw:
            try:
                number = self.recipient
                recipient_hint = f"***{number[-4:]}"
            except ValueError:
                recipient_hint = "invalid"
        with self._lock:
            return {
                "enabled": self.enabled,
                "configured": self.configured,
                "credentials_present": self.credentials_present,
                "graph_version": self.graph_version,
                "template_name": self.template_name,
                "template_language": self.template_language,
                "recipient": recipient_hint,
                "last_message_id": self.last_message_id,
                "last_sent_at": (
                    self.last_sent_at.isoformat() if self.last_sent_at else None
                ),
                "last_error": self.last_error,
            }

    def send_test(self) -> dict[str, Any]:
        """Send Meta's pre-approved hello_world template for setup validation."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.recipient,
            "type": "template",
            "template": {
                "name": self.test_template_name,
                "language": {"code": "en_US"},
            },
        }
        return self._send(payload)

    def notify(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Send one approved Trade M alert template for a newly-created event."""
        if not self.enabled:
            return None
        if not self.credentials_present:
            raise WhatsAppError(
                "WhatsApp alerts are enabled but access token, phone number ID, "
                "recipient, or template name is missing."
            )

        parameters = [
            f"{event.get('exchange', '')}:{event.get('tradingsymbol', '')}",
            str(event.get("trade_side") or event.get("direction") or "ALERT"),
            str(event.get("threshold_display") or event.get("threshold") or ""),
            str(event.get("candle_close") or ""),
            str(event.get("stop_loss_display") or event.get("stop_loss") or ""),
            str(event.get("risk_percent_display") or event.get("risk_percent") or ""),
            self._event_time(event),
            str(event.get("provider") or "").title(),
        ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.recipient,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value} for value in parameters
                        ],
                    }
                ],
            },
        }
        return self._send(payload)

    @staticmethod
    def _event_time(event: dict[str, Any]) -> str:
        raw = event.get("candle_end") or event.get("created_at")
        if not raw:
            return datetime.now(IST).strftime("%d %b %Y %H:%M")
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(IST).strftime("%d %b %Y %H:%M")
        except ValueError:
            return str(raw)

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise WhatsAppError(
                "WhatsApp is not fully configured. Set WHATSAPP_ENABLED=true plus "
                "WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, and WHATSAPP_TO."
            )

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        last_error: WhatsAppError | None = None

        for attempt in range(1, 4):
            try:
                response = self.session.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = WhatsAppError(f"WhatsApp network request failed: {exc}")
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
                    continue
                break

            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text[:1000]}

            message_id = self._message_id(data)
            if response.ok and message_id:
                with self._lock:
                    self.last_error = None
                    self.last_message_id = message_id
                    self.last_sent_at = datetime.now(IST)
                logger.info("WhatsApp accepted Trade M message %s", message_id)
                return data

            error = data.get("error") if isinstance(data, dict) else None
            error_code = error.get("code") if isinstance(error, dict) else None
            detail = self._error_detail(response.status_code, data)
            last_error = WhatsAppError(detail)
            retryable = (
                response.status_code == 429
                or response.status_code >= 500
                or error_code in _RETRYABLE_META_CODES
            )
            if retryable and attempt < 3:
                time.sleep(2 ** (attempt - 1))
                continue
            break

        assert last_error is not None
        with self._lock:
            self.last_error = str(last_error)
        logger.error("%s", last_error)
        raise last_error

    @staticmethod
    def _message_id(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        message = messages[0]
        if not isinstance(message, dict):
            return None
        value = message.get("id")
        return str(value) if value else None

    @staticmethod
    def _error_detail(status_code: int, data: Any) -> str:
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            error = data["error"]
            code = error.get("code")
            message = error.get("message") or error.get("type") or "Unknown Meta error"
            details = error.get("error_data")
            suffix = ""
            if isinstance(details, dict) and details.get("details"):
                suffix = f" — {details['details']}"
            code_text = f" code {code}" if code is not None else ""
            return f"WhatsApp API HTTP {status_code}{code_text}: {message}{suffix}"
        return f"WhatsApp API HTTP {status_code}: {data}"
