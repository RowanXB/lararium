import itertools
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from lararium import __version__
from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret
from lararium.modules.finance.ibkr.client import (
    FlexClient,
    FlexConfigError,
    FlexError,
    FlexRetryableError,
)

TOKEN = "tok-9f8e7d6c5b4a"
REF = "1234567890"
STATEMENT = (
    b'<FlexQueryResponse queryName="q" type="AF"><FlexStatements count="0"/></FlexQueryResponse>'
)


def send_ok(url_tag: str = "Url") -> bytes:
    return (
        f"<FlexStatementResponse timestamp='x'><Status>Success</Status>"
        f"<ReferenceCode>{REF}</ReferenceCode>"
        f"<{url_tag}>https://example.invalid/GetStatement</{url_tag}>"
        f"</FlexStatementResponse>"
    ).encode()


def failure(code: int, message: str = "msg") -> bytes:
    return (
        f"<FlexStatementResponse timestamp='x'><Status>Warn</Status>"
        f"<ErrorCode>{code}</ErrorCode><ErrorMessage>{message}</ErrorMessage>"
        f"</FlexStatementResponse>"
    ).encode()


class Env:
    """A fake clock that sleeping advances, and a transport that records each request."""

    def __init__(self, *responses: bytes, repeat_last: bool = False) -> None:
        self.now = Decimal(1000)
        self.sleeps: list[int] = []
        self.requests: list[tuple[Decimal, httpx.Request]] = []
        self._responses = list(responses)
        self._repeat_last = repeat_last

    async def sleep(self, seconds: int) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.now, request))
        body = self._responses[0] if self._repeat_last else self._responses.pop(0)
        return httpx.Response(200, content=body)

    def client(self) -> tuple[FlexClient, httpx.AsyncClient]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return FlexClient(http, Secret(TOKEN), sleep=self.sleep, clock=self.clock), http


async def fetch(env: Env) -> bytes:
    client, http = env.client()
    async with http:
        return await client.fetch("777", date(2026, 1, 1), date(2026, 1, 31))


async def test_send_request_params() -> None:
    env = Env(send_ok(), STATEMENT)
    assert await fetch(env) == STATEMENT
    send = env.requests[0][1]
    params = parse_qs(send.url.query.decode())
    assert send.url.path.endswith("/FlexWebService/SendRequest")
    assert params == {
        "t": [TOKEN],
        "q": ["777"],
        "v": ["3"],
        "fd": ["20260101"],
        "td": ["20260131"],
    }
    assert send.headers["User-Agent"] == f"Lararium/{__version__}"
    get = env.requests[1][1]
    assert get.url.path.endswith("/FlexWebService/GetStatement")
    assert parse_qs(get.url.query.decode()) == {"t": [TOKEN], "q": [REF], "v": ["3"]}
    assert get.headers["User-Agent"] == f"Lararium/{__version__}"


async def test_poll_until_ready() -> None:
    env = Env(send_ok(), failure(1019), failure(1019), STATEMENT)
    assert await fetch(env) == STATEMENT
    assert env.sleeps == [5, 6, 6]
    times = [t for t, _ in env.requests]
    assert [b - a for a, b in itertools.pairwise(times)] == [5, 6, 6]


async def test_not_ready_is_ordinary_failure() -> None:
    env = Env(send_ok(), failure(1003), repeat_last=False)
    with pytest.raises(FlexRetryableError) as caught:
        await fetch(env)
    assert not isinstance(caught.value, CredentialRejectedError)
    assert caught.value.code == 1003


async def test_poll_budget_exhausted_is_retryable() -> None:
    class Endless(Env):
        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append((self.now, request))
            first = len(self.requests) == 1
            return httpx.Response(200, content=send_ok() if first else failure(1019))

    env = Endless()
    with pytest.raises(FlexRetryableError) as caught:
        await fetch(env)
    assert caught.value.code == 1019
    assert env.now - 1000 <= 120
    assert sum(env.sleeps) > 100


@pytest.mark.parametrize("code", range(1011, 1017))
async def test_config_errors_are_credential_rejected(code: int) -> None:
    env = Env(failure(code, "Token has expired."))
    with pytest.raises(FlexConfigError) as caught:
        await fetch(env)
    assert isinstance(caught.value, CredentialRejectedError)
    assert str(caught.value) == f"IBKR Flex error {code}: Token has expired."


@pytest.mark.parametrize("code", [1001, 1004, 1005, 1006, 1007, 1008, 1009, 1018, 1021])
async def test_retryable_codes(code: int) -> None:
    env = Env(failure(code))
    with pytest.raises(FlexRetryableError) as caught:
        await fetch(env)
    assert str(caught.value) == f"IBKR Flex error {code}"


async def test_unknown_code_is_plain_flex_error() -> None:
    env = Env(failure(1099))
    with pytest.raises(FlexError) as caught:
        await fetch(env)
    assert type(caught.value) is FlexError
    assert caught.value.code == 1099


@pytest.mark.parametrize("tag", ["Url", "url"])
async def test_url_element_any_case(tag: str) -> None:
    env = Env(send_ok(tag), STATEMENT)
    assert await fetch(env) == STATEMENT
    assert all(r.url.host == "ndcdyn.interactivebrokers.com" for _, r in env.requests)


async def test_doctype_refused() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
        b"<FlexQueryResponse>&a;</FlexQueryResponse>"
    )
    env = Env(send_ok(), bomb)
    with pytest.raises(FlexError) as caught:
        await fetch(env)
    assert caught.value.code == 0


async def test_malformed_body_is_flex_error() -> None:
    env = Env(b"<html>oops")
    with pytest.raises(FlexError) as caught:
        await fetch(env)
    assert caught.value.code == 0


async def test_errors_never_contain_token() -> None:
    echoed = f"bad token {TOKEN} at https://x.invalid/?t={TOKEN}"
    bodies: list[tuple[list[bytes], int]] = [
        ([failure(1015, echoed)], 1015),
        ([failure(1099, echoed)], 1099),
        ([b"<html>" + TOKEN.encode()], 0),
    ]
    for responses, _ in bodies:
        env = Env(*responses)
        with pytest.raises(FlexError) as caught:
            await fetch(env)
        for text in (str(caught.value), repr(caught.value), repr(caught.value.args)):
            assert TOKEN not in text
            assert "http" not in text

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    env = Env()
    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = FlexClient(http, Secret(TOKEN), sleep=env.sleep, clock=env.clock)
    async with http:
        with pytest.raises(FlexRetryableError) as caught:
            await client.fetch("1", date(2026, 1, 1), date(2026, 1, 2))
    assert TOKEN not in str(caught.value) + repr(caught.value)
    assert caught.value.__cause__ is None


async def test_spacing_across_calls() -> None:
    env = Env(send_ok(), STATEMENT, send_ok(), failure(1019), STATEMENT)
    client, http = env.client()
    async with http:
        await client.fetch("1", date(2026, 1, 1), date(2026, 1, 2))
        await client.fetch("1", date(2026, 1, 3), date(2026, 1, 4))
    assert len(env.requests) == 5
    for (t0, r0), (t1, r1) in itertools.pairwise(env.requests):
        first_poll = r0.url.path.endswith("SendRequest") and r1.url.path.endswith("GetStatement")
        # The first poll follows its SendRequest after 5 s; any other two requests, 6 s.
        assert t1 - t0 >= (5 if first_poll else 6)
