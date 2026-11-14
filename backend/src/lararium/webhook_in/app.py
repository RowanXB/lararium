"""The webhook receiver: stores each Monzo delivery in core.webhook_inbox, and does no more.

It is the only part of Lararium reachable from the internet, so it is kept small: one route,
one parameterised INSERT through a plain psycopg pool, and a database role that can insert
into the inbox and nothing else (migration 0011). The worker turns stored deliveries into
syncs (lararium.worker.jobs.process_webhook_inbox). It needs neither the master key nor any
finance setting, and imports nothing from Lararium beyond config and db.

Monzo's deliveries are unsigned, so the secret in the path is what tells ours from others':
a wrong one gets the same 404 as any unknown path. The secret must stay out of access logs,
hence `python -m lararium.webhook_in`, which runs uvicorn without them.
"""

import hmac
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, LiteralString, NoReturn, cast

import psycopg
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from lararium.config import WebhookInSettings
from lararium.db import STATEMENT_TIMEOUT, psycopg_dsn

MAX_BODY_BYTES = 64 * 1024
# Deeper than any real delivery. Fixed here because Python's own parser limit varies by version.
MAX_JSON_DEPTH = 64
# No RETURNING: the receiver's role can't SELECT.
INSERT: LiteralString = "INSERT INTO core.webhook_inbox (source, body) VALUES ('monzo', %s::jsonb)"


def _not_json(constant: str) -> NoReturn:
    # NaN and Infinity, which Python accepts but JSON (and jsonb) doesn't.
    raise ValueError(f"{constant} is not JSON")


def _too_deep(value: Any) -> bool:
    """Whether JSON nesting exceeds MAX_JSON_DEPTH. Iterative, so hostile depth can't recurse."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, dict | list):
            continue
        if depth > MAX_JSON_DEPTH:
            return True
        children: list[Any] = (
            list(cast("dict[str, Any]", item).values())
            if isinstance(item, dict)
            else cast("list[Any]", item)
        )
        stack.extend((child, depth + 1) for child in children)
    return False


async def _read_body(request: Request) -> bytes | None:
    """The request body, or None once it is over MAX_BODY_BYTES (read no further)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def create_app(database_url: str, secret: SecretStr) -> Starlette:
    expected = secret.get_secret_value().encode()
    pool = AsyncConnectionPool(
        psycopg_dsn(database_url),
        open=False,
        min_size=1,
        max_size=4,
        kwargs={"options": f"-c statement_timeout={STATEMENT_TIMEOUT}"},
    )

    async def monzo(request: Request) -> Response:
        given = str(request.path_params["secret"]).encode()
        if not hmac.compare_digest(given, expected):
            return Response(status_code=404)
        body = await _read_body(request)
        if body is None:
            return Response(status_code=413)
        try:
            document = body.decode()
            parsed = json.loads(document, parse_constant=_not_json)
        except (ValueError, RecursionError):
            # UnicodeDecodeError and JSONDecodeError are ValueErrors; RecursionError is nesting
            # too deep to parse.
            return Response(status_code=400)
        if _too_deep(parsed):
            return Response(status_code=400)
        try:
            async with pool.connection() as conn:
                await conn.execute(INSERT, (document,))
        except (
            psycopg.DataError,
            psycopg.errors.ProgramLimitExceeded,
            psycopg.errors.StatementTooComplex,
        ):
            # JSON that jsonb refuses, such as a \u0000 in a string, or that is too complex
            # for Postgres (a backstop for MAX_JSON_DEPTH).
            return Response(status_code=400)
        return Response(status_code=200)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        await pool.open()
        try:
            yield
        finally:
            await pool.close()

    return Starlette(routes=[Route("/monzo/{secret}", monzo, methods=["POST"])], lifespan=lifespan)


def from_env() -> Starlette:
    settings = WebhookInSettings()  # pyright: ignore[reportCallIssue]  # read from LARARIUM_* env vars
    return create_app(settings.database_url, settings.webhook_secret)


def __getattr__(name: str) -> Starlette:
    # `app`, built from the environment when first looked up (as uvicorn does), so importing
    # this module needs no settings.
    if name == "app":
        return from_env()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
