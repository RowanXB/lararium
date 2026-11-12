"""Notifications to the owner. Messages name the connection and the problem, never credentials."""

from typing import Protocol


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    """Sends nothing: used when no notification channel is configured."""

    async def send(self, text: str) -> None:
        return None
