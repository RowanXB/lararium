"""Notifications to the owner. Messages name the connection and the problem, never credentials."""

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx
from pydantic import SecretStr

from lararium.config import Settings

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    """Sends nothing: used when no notification channel is configured."""

    async def send(self, text: str) -> None:
        return None


def _unix_now() -> int:
    return int(time.time())


class FeishuNotifier:
    """Posts text messages to a Feishu custom bot, signed when a secret is configured.

    Failures are logged without the webhook URL and never raised: a notification must not
    fail the job that sends it.
    """

    def __init__(
        self,
        url: SecretStr,
        secret: SecretStr | None,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], int] = _unix_now,
    ) -> None:
        self._url = url
        self._secret = secret
        self._http = http
        self._clock = clock

    def _payload(self, text: str) -> dict[str, object]:
        payload: dict[str, object] = {"msg_type": "text", "content": {"text": text}}
        if self._secret is not None:
            timestamp = str(self._clock())
            key = f"{timestamp}\n{self._secret.get_secret_value()}".encode()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
        return payload

    async def send(self, text: str) -> None:
        try:
            response = await self._http.post(self._url.get_secret_value(), json=self._payload(text))
        except httpx.HTTPError as exc:
            # The exception text can contain the URL, so log only its type.
            logger.warning("Feishu notification failed: %s", type(exc).__name__)
            return
        if response.status_code != 200:
            logger.warning("Feishu notification failed: HTTP %s", response.status_code)
            return
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            logger.warning("Feishu notification failed: response was not a JSON object")
            return
        if code != 0:
            logger.warning("Feishu notification rejected: code %s", code)


def notifier_from_settings(settings: Settings, http: httpx.AsyncClient) -> Notifier:
    url = settings.feishu_webhook_url
    if url is None or not url.get_secret_value():
        return NullNotifier()
    secret = settings.feishu_secret
    if secret is not None and not secret.get_secret_value():
        secret = None
    return FeishuNotifier(url, secret, http)
