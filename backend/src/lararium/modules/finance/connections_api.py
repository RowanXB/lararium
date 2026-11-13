"""/api/v1/connections routes and the Monzo OAuth flow (Decision 7).

Each route's name is its OpenAPI operationId. Credentials (the client secret, tokens) and
the OAuth code and state never reach a response, the audit log or the logs.
"""

import logging
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    ConnectionRecord,
    ConnectionStatus,
    consume_oauth_state,
    create_connection,
    get_connection,
    list_connections,
    load_credential,
    set_status,
    store_credential,
    store_oauth_state,
    transition_status,
)
from lararium.core.errors import ConnectionNotFoundError, CredentialRejectedError
from lararium.core.secrets import load_master_key
from lararium.deps import (
    ActorDep,
    SessionDep,
    SessionFactoryDep,
    get_actor,
    get_actor_allowing_navigation,
)
from lararium.modules.finance.monzo.auth import authorize_url, exchange_code, save_tokens
from lararium.modules.finance.monzo.client import MonzoApiError
from lararium.worker.app import defer_await_approval, defer_sync

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/connections"
CALLBACK_PATH = "/oauth/monzo/callback"
STATE_VALID_FOR = timedelta(minutes=10)
# Where the callback sends the browser back to: the web app's Connections page.
DONE = "/connections"
# Authorising may start only where no sync runs (run_sync syncs healthy and failing ones):
# the new tokens are saved outside the sync lock, so a sync's refresh could overwrite them.
# Disabled is included because authorising again is how a connection is re-enabled.
AUTHORISABLE: tuple[ConnectionStatus, ...] = ("created", "needs_reauth", "authorising", "disabled")

router = APIRouter(prefix=PREFIX, tags=["connections"], dependencies=[Depends(get_actor)])
# The callback alone may be a cross-site navigation (from auth.monzo.com).
callback_router = APIRouter(prefix=PREFIX, tags=["connections"])


class ConnectionOut(BaseModel):
    id: UUID
    connector: str
    display_name: str
    status: ConnectionStatus
    last_success_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    credential_expires_at: datetime | None
    warnings: list[str]
    # To register with the provider (developers.monzo.com); None until the server knows its
    # public URL (LARARIUM_PUBLIC_BASE_URL).
    redirect_uri: str | None
    webhook_enabled: bool


class MonzoConnectionCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: SecretStr = Field(min_length=1, max_length=500)


class AuthorizeOut(BaseModel):
    url: str


class SyncQueuedOut(BaseModel):
    # False when a sync was already queued.
    queued: bool


async def get_http() -> AsyncIterator[httpx.AsyncClient]:
    """The client for calls to the provider; tests replace it."""
    async with httpx.AsyncClient(timeout=30) as http:
        yield http


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(callback_router)
    app.add_exception_handler(ConnectionNotFoundError, _connection_not_found)


async def _connection_not_found(request: Request, exc: Exception) -> JSONResponse:
    message = f"no connection has the id {exc}"
    return JSONResponse(
        {"detail": {"error": "connection_not_found", "message": message}},
        status.HTTP_404_NOT_FOUND,
    )


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _not_configured(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, {"error": "not_configured", "message": message}
    )


def _redirect_uri(settings: Settings) -> str | None:
    if not settings.public_base_url:
        return None
    return f"{settings.public_base_url}{PREFIX}{CALLBACK_PATH}"


def _require_redirect_uri(settings: Settings) -> str:
    redirect_uri = _redirect_uri(settings)
    if redirect_uri is None:
        raise _not_configured(
            "set LARARIUM_PUBLIC_BASE_URL to where your browser reaches Lararium,"
            " so Monzo can send you back after you authorise"
        )
    return redirect_uri


def _require_master_key(settings: Settings) -> bytes:
    if settings.master_key is None or not settings.master_key.get_secret_value():
        raise _not_configured("set LARARIUM_MASTER_KEY, which encrypts stored credentials")
    return load_master_key(settings.master_key)


def _out(record: ConnectionRecord, settings: Settings) -> ConnectionOut:
    return ConnectionOut(
        id=record.id,
        connector=record.connector,
        display_name=record.display_name,
        status=record.status,
        last_success_at=record.last_success_at,
        last_error=record.last_error,
        consecutive_failures=record.consecutive_failures,
        credential_expires_at=record.credential_expires_at,
        warnings=record.warnings,
        redirect_uri=_redirect_uri(settings),
        # No webhook receiver is configured yet: the 30-minute schedule is the only trigger.
        webhook_enabled=False,
    )


@router.get("", name="list_connections")
async def list_connections_route(request: Request, session: SessionDep) -> list[ConnectionOut]:
    settings = _settings(request)
    return [_out(record, settings) for record in await list_connections(session)]


@router.post("", name="create_monzo_connection", status_code=status.HTTP_201_CREATED)
async def create_monzo_connection(
    body: MonzoConnectionCreate, request: Request, session: SessionDep, actor: ActorDep
) -> ConnectionOut:
    settings = _settings(request)
    master_key = _require_master_key(settings)
    _require_redirect_uri(settings)
    args = body.model_dump(mode="json", exclude={"client_secret"})
    async with command_context(session, actor, "create_monzo_connection", args) as ctx:
        record = await create_connection(
            ctx,
            connector="monzo",
            display_name=body.display_name,
            config={"client_id": body.client_id},
        )
        await store_credential(
            ctx,
            master_key,
            record.id,
            "client_secret",
            body.client_secret.get_secret_value(),
            None,
        )
    return _out(record, settings)


@router.post("/{connection_id}/authorize", name="authorize_connection")
async def authorize_connection(
    connection_id: UUID, request: Request, session: SessionDep, actor: ActorDep
) -> AuthorizeOut:
    redirect_uri = _require_redirect_uri(_settings(request))
    connection = await get_connection(session, connection_id)
    client_id = connection.config.get("client_id")
    if connection.connector != "monzo" or not isinstance(client_id, str):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"error": "not_authorisable", "message": "this connection has no Monzo client id"},
        )
    if connection.status not in AUTHORISABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "error": "not_authorisable",
                "message": f"a {connection.status} connection can't be authorised again;"
                " authorise it once it needs it",
            },
        )
    state = secrets.token_urlsafe(32)
    args = {"connection_id": str(connection_id)}
    async with command_context(session, actor, "authorize_connection", args) as ctx:
        await store_oauth_state(ctx, connection_id, state, STATE_VALID_FOR)
    return AuthorizeOut(url=authorize_url(client_id, redirect_uri, state))


@callback_router.get(
    CALLBACK_PATH,
    name="monzo_oauth_callback",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def monzo_oauth_callback(
    request: Request,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    http: HttpDep,
    actor: Annotated[Actor, Depends(get_actor_allowing_navigation)],
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    """Where Monzo sends the browser back: exchange the code, then wait for approval."""
    settings = _settings(request)
    master_key = _require_master_key(settings)
    redirect_uri = _require_redirect_uri(settings)
    if not code or not state:
        return _done("state")
    # Its own short transaction: the state is used up even if the exchange then fails.
    async with command_context(session, actor, "consume_oauth_state", {}) as ctx:
        connection_id = await consume_oauth_state(ctx, state)
    if connection_id is None:
        return _done("state")

    connection = await get_connection(session, connection_id)
    if connection.status not in AUTHORISABLE:
        return _done("status")
    client_id = connection.config.get("client_id")
    client_secret = await load_credential(session, master_key, connection_id, "client_secret")
    if not isinstance(client_id, str) or client_secret is None:
        return _done("exchange")
    try:
        pair = await exchange_code(http, client_id, client_secret, redirect_uri, code)
    except (CredentialRejectedError, MonzoApiError, httpx.HTTPError) as exc:
        # The type only: an HTTP error's text can include the request.
        logger.warning("Monzo code exchange failed: %s", type(exc).__name__)
        return _done("exchange")
    await save_tokens(session_factory, master_key, connection_id, pair, actor=actor)

    args = {"connection_id": str(connection_id)}
    async with command_context(session, actor, "monzo_oauth_callback", args) as ctx:
        moved = await transition_status(ctx, connection_id, "authorising", only_from=AUTHORISABLE)
    if moved is None:
        return _done("status")
    # After the commit, so the job finds the connection authorising.
    await defer_await_approval(connection_id)
    return _done()


def _done(error: str | None = None) -> RedirectResponse:
    url = DONE if error is None else f"{DONE}?error={error}"
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/{connection_id}/sync", name="sync_connection_now", status_code=status.HTTP_202_ACCEPTED
)
async def sync_connection_now(connection_id: UUID, session: SessionDep) -> SyncQueuedOut:
    await get_connection(session, connection_id)
    return SyncQueuedOut(queued=await defer_sync(connection_id))


@router.post("/{connection_id}/disable", name="disable_connection")
async def disable_connection(
    connection_id: UUID, request: Request, session: SessionDep, actor: ActorDep
) -> ConnectionOut:
    args = {"connection_id": str(connection_id)}
    async with command_context(session, actor, "disable_connection", args) as ctx:
        record = await set_status(ctx, connection_id, "disabled")
    return _out(record, _settings(request))
