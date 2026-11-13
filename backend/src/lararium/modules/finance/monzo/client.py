"""A thin client for the Monzo API (docs.monzo.com); amounts stay in integer minor units.

Error messages carry the HTTP status and Monzo's error code only, never a token or the
response body, which an upstream could fill with anything it was sent.
"""

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

from lararium.core.secrets import Secret

API = "https://api.monzo.com"
AUTH = "https://auth.monzo.com/"
# Monzo's maximum page size for GET /transactions.
PAGE_SIZE = 100

# Monzo error codes look like "unauthorized.bad_access_token.expired"; anything else is
# replaced, so an echoed token can't reach a message.
_CODE = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)*")
_MAX_CODE_LENGTH = 100


class MonzoApiError(Exception):
    """Monzo answered with an error status."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"Monzo API error {status}: {code}")
        self.status = status
        self.code = code


class AccessTokenExpiredError(MonzoApiError):
    """The access token is expired or revoked (401 invalid_token / unauthorized)."""


class NotYetApprovedError(MonzoApiError):
    """403: the owner hasn't yet approved this token in the Monzo app."""


class MonzoRateLimitedError(MonzoApiError):
    """429: too many requests."""


def error_code(response: httpx.Response) -> str:
    """Monzo's error code from the body (`error` or `code`), or "unknown"."""
    try:
        body = response.json()
    except ValueError:
        return "unknown"
    if not isinstance(body, dict):
        return "unknown"
    fields: dict[str, Any] = body  # pyright: ignore[reportUnknownVariableType]
    for key in ("error", "code"):
        value = fields.get(key)
        if isinstance(value, str) and len(value) <= _MAX_CODE_LENGTH and _CODE.fullmatch(value):
            return value
    return "unknown"


def raise_for_error(response: httpx.Response) -> None:
    """Raise the MonzoApiError that matches an error response; do nothing on success."""
    status = response.status_code
    if status < 400:
        return
    code = error_code(response)
    if status == 401 and code.split(".", 1)[0] in ("invalid_token", "unauthorized"):
        raise AccessTokenExpiredError(status, code)
    if status == 403:
        raise NotYetApprovedError(status, code)
    if status == 429:
        raise MonzoRateLimitedError(status, code)
    raise MonzoApiError(status, code)


type Params = Sequence[tuple[str, str]]


class MonzoClient:
    def __init__(self, http: httpx.AsyncClient, access_token: Secret) -> None:
        self._http = http
        self._access_token = access_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Params = (),
        data: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._http.request(
            method,
            f"{API}{path}",
            params=list(params),
            data=data,
            headers={"Authorization": f"Bearer {self._access_token.reveal()}"},
        )
        raise_for_error(response)
        if not response.content:
            return {}
        body: dict[str, Any] = response.json()
        return body

    async def whoami(self) -> dict[str, Any]:
        return await self._request("GET", "/ping/whoami")

    async def accounts(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/accounts"))["accounts"]

    async def balance(self, account_id: str) -> dict[str, Any]:
        return await self._request("GET", "/balance", params=[("account_id", account_id)])

    async def pots(self, account_id: str) -> list[dict[str, Any]]:
        params = [("current_account_id", account_id)]
        return (await self._request("GET", "/pots", params=params))["pots"]

    async def transactions(
        self, account_id: str, *, since: str | None, before: str | None = None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield pages of up to 100 transactions, oldest first, from `since` (a timestamp or a
        transaction id) until a short page; each next page starts after the last id."""
        while True:
            params = [
                ("account_id", account_id),
                ("expand[]", "merchant"),
                ("limit", str(PAGE_SIZE)),
            ]
            if since is not None:
                params.append(("since", since))
            if before is not None:
                params.append(("before", before))
            page: list[dict[str, Any]] = (
                await self._request("GET", "/transactions", params=params)
            )["transactions"]
            if page:
                yield page
            if len(page) < PAGE_SIZE:
                return
            since = page[-1]["id"]

    async def webhooks(self, account_id: str) -> list[dict[str, Any]]:
        params = [("account_id", account_id)]
        return (await self._request("GET", "/webhooks", params=params))["webhooks"]

    async def register_webhook(self, account_id: str, url: str) -> dict[str, Any]:
        data = {"account_id": account_id, "url": url}
        return (await self._request("POST", "/webhooks", data=data))["webhook"]

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhooks/{webhook_id}")
