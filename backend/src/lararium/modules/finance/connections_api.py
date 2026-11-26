"""/api/v1/connections routes and the Monzo OAuth flow (Decision 7).

Each route's name is its OpenAPI operationId. Credentials (the client secret, tokens) and
the OAuth code and state never reach a response, the audit log or the logs.
"""

import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.config import Settings, load_master_key
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    AUTHORISED_AT_KEY,
    ConnectionRecord,
    ConnectionStatus,
    consume_oauth_state,
    create_connection,
    get_connection,
    list_connections,
    load_credential,
    lock_connection,
    set_credential_expiry,
    set_cursor,
    set_status,
    store_credential,
    store_oauth_state,
    transition_status,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.core.errors import (
    ConnectionNotFoundError,
    CredentialDecryptError,
    CredentialRejectedError,
)
from lararium.core.json_types import JSONValue
from lararium.core.notify import notifier_from_settings
from lararium.core.secrets import Secret
from lararium.deps import (
    ActorDep,
    DocumentStoreDep,
    SessionDep,
    SessionFactoryDep,
    get_actor,
    get_actor_allowing_navigation,
)
from lararium.modules.finance import models, transfers
from lararium.modules.finance.api import read_upload
from lararium.modules.finance.errors import ReviewNotFoundError
from lararium.modules.finance.etoro.client import GATE_MESSAGE, EtoroClient, ReadOnlyGateError
from lararium.modules.finance.etoro.normalize import EtoroDataError
from lararium.modules.finance.etoro.normalize import normalize as etoro_normalize
from lararium.modules.finance.etoro.statement import StatementError
from lararium.modules.finance.etoro.sync import (
    KEY_PURPOSE as ETORO_KEY_PURPOSE,
)
from lararium.modules.finance.etoro.sync import (
    USER_KEY_PURPOSE as ETORO_USER_KEY_PURPOSE,
)
from lararium.modules.finance.etoro.sync import (
    EtoroSyncError,
    StatementRejected,
    StatementTooEarly,
    me_ids,
)
from lararium.modules.finance.etoro.sync import (
    ingest_statement as ingest_etoro_statement,
)
from lararium.modules.finance.etoro.sync import (
    parse_upload as parse_etoro_upload,
)
from lararium.modules.finance.etoro.sync import (
    publish_replayed as etoro_publish_replayed,
)
from lararium.modules.finance.etoro.xlsx import XlsxError
from lararium.modules.finance.ibkr.normalize import normalize
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.sync import (
    EXPIRY_NOTIFIED_KEY,
    LONDON,
    TOKEN_PURPOSE,
    ingest_statement,
)
from lararium.modules.finance.monzo.auth import authorize_url, exchange_code, store_tokens
from lararium.modules.finance.monzo.client import MonzoApiError
from lararium.modules.finance.readonly_http import HttpAuthError, HttpError
from lararium.modules.finance.schemas import ReviewOut, ReviewResolve
from lararium.modules.finance.source_reviews import list_reviews, resolve_review
from lararium.modules.finance.trading212.client import Trading212Client
from lararium.modules.finance.trading212.export import ExportError, ExportTooEarly
from lararium.modules.finance.trading212.export import (
    ingest_export as ingest_trading212_export,
)
from lararium.modules.finance.trading212.export import (
    parse_upload as parse_trading212_upload,
)
from lararium.modules.finance.trading212.normalize import Trading212DataError
from lararium.modules.finance.trading212.normalize import normalize as trading212_normalize
from lararium.modules.finance.trading212.sync import (
    KEY_PURPOSE as T212_KEY_PURPOSE,
)
from lararium.modules.finance.trading212.sync import (
    SECRET_PURPOSE as T212_SECRET_PURPOSE,
)
from lararium.modules.finance.trading212.sync import (
    Trading212SyncError,
    summary_account_id,
)
from lararium.modules.finance.trading212.sync import (
    publish_replayed as trading212_publish_replayed,
)
from lararium.modules.finance.transfers_api import LOCK_TIMEOUT, healing_command
from lararium.worker.app import defer_await_approval, defer_sync

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/connections"
CALLBACK_PATH = "/oauth/monzo/callback"
STATE_VALID_FOR = timedelta(minutes=10)
# Where the callback sends the browser back to: the web app's Connections page.
DONE = "/connections"
# Authorising may start only where no regular sync runs (run_sync syncs healthy and failing
# ones): the new tokens are saved outside the sync lock, so such a sync's refresh could
# overwrite them. `authorising` covers the wait for approval and the backfill after it; a new
# authorisation then replaces the one waited on, and every decision about the old one is
# dropped (see AUTHORISED_AT_KEY). Disabled is included because authorising again is how a
# connection is re-enabled.
AUTHORISABLE: tuple[ConnectionStatus, ...] = ("created", "needs_reauth", "authorising", "disabled")

# A one-year, all-sections, three-account Flex statement is about 10 MB: M2's 5 MiB is too small.
MAX_IBKR_UPLOAD_BYTES = 64 * 1024 * 1024
# An eToro account statement of several years is well under 1 MiB.
MAX_ETORO_UPLOAD_BYTES = 16 * 1024 * 1024
# A Trading 212 export is a line per event: years of them are well under 1 MiB.
MAX_TRADING212_UPLOAD_BYTES = 16 * 1024 * 1024
XML_TYPES = ("application/xml", "text/xml")
GENERIC_TYPE = "application/octet-stream"
QUERY_ID_PATTERN = r"^[0-9]{1,12}$"

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


class _IbkrToken(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=500)
    # The last day the token works, as Client Portal shows it.
    token_expires_on: date


class IbkrConnectionUpdate(_IbkrToken):
    # Omitted or null keeps the stored query id.
    query_id: str | None = Field(default=None, pattern=QUERY_ID_PATTERN)


class IbkrConnectionCreate(_IbkrToken):
    display_name: str = Field(min_length=1, max_length=200)
    query_id: str = Field(pattern=QUERY_ID_PATTERN)


class Trading212ConnectionUpdate(BaseModel):
    api_key: SecretStr = Field(min_length=1, max_length=500)
    api_secret: SecretStr = Field(min_length=1, max_length=500)


class Trading212ConnectionIn(Trading212ConnectionUpdate):
    display_name: str = Field(min_length=1, max_length=200)
    account_kind: Literal["invest", "isa"]


class EtoroConnectionUpdate(BaseModel):
    public_api_key: SecretStr = Field(min_length=1, max_length=500)
    user_key: SecretStr = Field(min_length=1, max_length=500)


class EtoroConnectionIn(EtoroConnectionUpdate):
    display_name: str = Field(min_length=1, max_length=200)


class EtoroUploadOut(BaseModel):
    # The statement rows the upload stored, once each.
    raw_records: int


class Trading212UploadOut(BaseModel):
    # The export rows the upload stored, once each.
    raw_records: int


class IbkrUploadOut(BaseModel):
    # The rows the statement holds, once each.
    raw_records: int


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
        # Only whether webhooks are configured: never the secret. Without them, the 30-minute
        # schedule is the only trigger.
        webhook_enabled=settings.webhook() is not None,
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
    try:
        client_secret = await load_credential(session, master_key, connection_id, "client_secret")
    except CredentialDecryptError:
        # Encrypted under another master key: no authorisation can use it, only adding the
        # connection again (with its secret) can.
        return _done("credentials")
    if not isinstance(client_id, str) or client_secret is None:
        return _done("exchange")
    try:
        pair = await exchange_code(http, client_id, client_secret, redirect_uri, code)
    except (CredentialRejectedError, MonzoApiError, httpx.HTTPError) as exc:
        # The type only: an HTTP error's text can include the request.
        logger.warning("Monzo code exchange failed: %s", type(exc).__name__)
        return _done("exchange")
    args = {"connection_id": str(connection_id)}
    async with command_context(session, actor, "monzo_oauth_callback", args) as ctx:
        # Under the row lock, and in one transaction: the tokens and the authorisation they
        # belong to are saved only if the connection can still be authorised, and together.
        # The previous authorisation's error (approve in time, authorise again) no longer applies.
        moved = await transition_status(
            ctx, connection_id, "authorising", only_from=AUTHORISABLE, clear_error=True
        )
        if moved is not None:
            await store_tokens(ctx, master_key, connection_id, pair)
            authorised_at = datetime.now(UTC).isoformat()
            await update_config(ctx, connection_id, {AUTHORISED_AT_KEY: authorised_at})
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


def _expires_at(token_expires_on: date) -> datetime:
    """The token works through its expiry day: it stops at the start of the next, in London.
    A day already past is refused."""
    if token_expires_on < datetime.now(LONDON).date():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"error": "expiry_in_past", "message": "the token's expiry date is in the past"},
        )
    stops = datetime.combine(token_expires_on + timedelta(days=1), time(0), tzinfo=LONDON)
    return stops.astimezone(UTC)


@router.post("/ibkr", name="create_ibkr_connection", status_code=status.HTTP_201_CREATED)
async def create_ibkr_connection(
    body: IbkrConnectionCreate, request: Request, session: SessionDep, actor: ActorDep
) -> ConnectionOut:
    settings = _settings(request)
    master_key = _require_master_key(settings)
    expires_at = _expires_at(body.token_expires_on)
    args = body.model_dump(mode="json", exclude={"token"})
    async with command_context(session, actor, "create_ibkr_connection", args) as ctx:
        created = await create_connection(
            ctx,
            connector="ibkr",
            display_name=body.display_name,
            config={"query_id": body.query_id},
        )
        await store_credential(
            ctx, master_key, created.id, TOKEN_PURPOSE, body.token.get_secret_value(), expires_at
        )
        await set_credential_expiry(ctx, created.id, expires_at)
        # There is no approval step: the token works as soon as it is created.
        record = await set_status(ctx, created.id, "healthy")
    # After the commit, so the job finds the connection.
    await defer_sync(record.id)
    return _out(record, settings)


# How a refusal names each connector: "this is not <name> connection".
_CONNECTOR_NAMES = {"ibkr": "an IBKR", "trading212": "a Trading 212", "etoro": "an eToro"}


def _not_connector(connection: ConnectionRecord, connector: str) -> HTTPException | None:
    """422 when the connection is not of this connector."""
    if connection.connector != connector:
        return _unprocessable(
            f"not_{connector}", f"this is not {_CONNECTOR_NAMES[connector]} connection"
        )
    return None


@router.put("/{connection_id}/ibkr", name="update_ibkr_connection")
async def update_ibkr_connection(
    connection_id: UUID,
    body: IbkrConnectionUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> ConnectionOut:
    """Replace the token, its expiry and the query id (a new token after the old one expired)."""
    settings = _settings(request)
    master_key = _require_master_key(settings)
    expires_at = _expires_at(body.token_expires_on)
    args = body.model_dump(mode="json", exclude={"token"}) | {"connection_id": str(connection_id)}
    async with command_context(session, actor, "update_ibkr_connection", args) as ctx:
        # Checked under the row lock: a disable made meanwhile can't slip in before the write.
        connection = await lock_connection(ctx, connection_id)
        if (refusal := _not_connector(connection, "ibkr")) is not None:
            raise refusal
        if connection.status == "disabled":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"error": "not_updatable", "message": "a disabled connection can't be updated"},
            )
        await store_credential(
            ctx, master_key, connection_id, TOKEN_PURPOSE, body.token.get_secret_value(), expires_at
        )
        await set_credential_expiry(ctx, connection_id, expires_at)
        # A new expiry is reminded in its turn.
        changes = {} if body.query_id is None else {"query_id": body.query_id}
        await update_config(ctx, connection_id, changes, remove=[EXPIRY_NOTIFIED_KEY])
        await transition_status(
            ctx, connection_id, "healthy", only_from=("needs_reauth",), clear_error=True
        )
        record = await get_connection(session, connection_id)
    await defer_sync(connection_id)
    return _out(record, settings)


def _unprocessable(error: str, message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, {"error": error, "message": message}
    )


async def _trading212_account_id(http: httpx.AsyncClient, body: Trading212ConnectionUpdate) -> str:
    """The account the key belongs to, from one account/summary call. Every message is fixed
    text: nothing the answer or the request holds can reach the owner or a log."""
    client = Trading212Client(
        http, Secret(body.api_key.get_secret_value()), Secret(body.api_secret.get_secret_value())
    )
    try:
        return summary_account_id(await client.summary())
    except HttpAuthError:
        raise _unprocessable("key_rejected", "Trading 212 rejected this key") from None
    except HttpError:
        raise _unprocessable(
            "key_not_checked",
            "Trading 212 could not check this key: it needs the account permission,"
            " and Trading 212 must be reachable. Try again.",
        ) from None
    except Trading212SyncError:
        raise _unprocessable("no_account_id", "Trading 212's answer has no account id") from None


async def _store_keys(
    ctx: CommandContext, master_key: bytes, connection_id: UUID, keys: Mapping[str, SecretStr]
) -> None:
    """Store each key under its credential purpose; none expires."""
    for purpose, value in keys.items():
        await store_credential(
            ctx, master_key, connection_id, purpose, value.get_secret_value(), None
        )


def _trading212_keys(body: Trading212ConnectionUpdate) -> dict[str, SecretStr]:
    return {T212_KEY_PURPOSE: body.api_key, T212_SECRET_PURPOSE: body.api_secret}


def _etoro_keys(body: EtoroConnectionUpdate) -> dict[str, SecretStr]:
    return {ETORO_KEY_PURPOSE: body.public_api_key, ETORO_USER_KEY_PURPOSE: body.user_key}


async def _create_with_keys(
    session: AsyncSession,
    actor: Actor,
    settings: Settings,
    *,
    connector: str,
    args: dict[str, JSONValue],
    display_name: str,
    config: dict[str, JSONValue],
    keys: Mapping[str, SecretStr],
    cursor: dict[str, JSONValue],
    identity: str,
    taken: str,
) -> ConnectionOut:
    """A connection whose keys work as soon as they are made (no approval step): refused 409
    with `taken` when another connection of the connector has the same `cursor[identity]`
    (the account or user the keys belong to); else created with its keys, its cursor where
    the sync's account check looks, healthy, and a sync queued after the commit."""
    master_key = _require_master_key(settings)
    async with command_context(session, actor, f"create_{connector}_connection", args) as ctx:
        # One create of a connector at a time: two at once would each pass the check below
        # before the other's connection is committed.
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"create_connection:{connector}")))
        )
        if any(
            isinstance(other.cursor, dict) and other.cursor.get(identity) == cursor[identity]
            for other in await list_connections(session)
            if other.connector == connector
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT, {"error": "already_connected", "message": taken}
            )
        created = await create_connection(
            ctx, connector=connector, display_name=display_name, config=config
        )
        await _store_keys(ctx, master_key, created.id, keys)
        await set_cursor(ctx, created.id, cursor)
        record = await set_status(ctx, created.id, "healthy")
    await defer_sync(record.id)
    return _out(record, settings)


async def _update_keys(
    session: AsyncSession,
    actor: Actor,
    settings: Settings,
    connection_id: UUID,
    connector: str,
    keys: Mapping[str, SecretStr],
    *,
    check: Callable[[], Awaitable[dict[str, JSONValue]]],
    identity: str,
    other: str,
) -> ConnectionOut:
    """Replace the connection's keys (new ones after the old were rejected): a needs_reauth
    connection is healthy again, and a sync is queued, whose own checks try the new keys.

    A disabled connection comes back this way only: its new keys first pass the checks of a
    create (`check`, which returns the identity the keys belong to, as the cursor keeps it),
    and must be the same account or user (`cursor[identity]`; else 422 with `other`)."""
    master_key = _require_master_key(settings)
    before = await get_connection(session, connection_id)
    checked = await check() if before.status == "disabled" else None
    args: dict[str, JSONValue] = {"connection_id": str(connection_id)}
    async with command_context(session, actor, f"update_{connector}_connection", args) as ctx:
        # Checked under the row lock: a disable made meanwhile can't slip in before the write.
        connection = await lock_connection(ctx, connection_id)
        if (refusal := _not_connector(connection, connector)) is not None:
            raise refusal
        if connection.status == "disabled":
            if checked is None:
                raise _unprocessable(
                    "not_updatable", "the connection was disabled meanwhile: try again"
                )
            cursor = connection.cursor if isinstance(connection.cursor, dict) else {}
            if cursor.get(identity) != checked[identity]:
                raise _unprocessable("other_identity", other)
        await _store_keys(ctx, master_key, connection_id, keys)
        await transition_status(
            ctx,
            connection_id,
            "healthy",
            only_from=("needs_reauth", "disabled") if checked is not None else ("needs_reauth",),
            clear_error=True,
        )
        record = await get_connection(session, connection_id)
    await defer_sync(connection_id)
    return _out(record, settings)


@router.post(
    "/trading212", name="create_trading212_connection", status_code=status.HTTP_201_CREATED
)
async def create_trading212_connection(
    body: Trading212ConnectionIn,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
) -> ConnectionOut:
    settings = _settings(request)
    _require_master_key(settings)
    account_id = await _trading212_account_id(http, body)
    return await _create_with_keys(
        session,
        actor,
        settings,
        connector="trading212",
        args=body.model_dump(mode="json", exclude={"api_key", "api_secret"}),
        display_name=body.display_name,
        config={"account_kind": body.account_kind},
        keys=_trading212_keys(body),
        cursor={"account_id": account_id},
        identity="account_id",
        taken="this Trading 212 account is already connected",
    )


@router.put("/{connection_id}/trading212", name="update_trading212_connection")
async def update_trading212_connection(
    connection_id: UUID,
    body: Trading212ConnectionUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
) -> ConnectionOut:
    """Replace the key and secret. The sync checks that they belong to the same account; a
    disabled connection is checked first, as a create is, and is healthy again."""

    async def check() -> dict[str, JSONValue]:
        return {"account_id": await _trading212_account_id(http, body)}

    return await _update_keys(
        session,
        actor,
        _settings(request),
        connection_id,
        "trading212",
        _trading212_keys(body),
        check=check,
        identity="account_id",
        other="these keys are another Trading 212 account's",
    )


def _upload_args(
    connection_id: UUID, file: UploadFile, body: bytes, default_name: str
) -> tuple[str, dict[str, JSONValue]]:
    """The upload's file name (its last path part, at most 200 characters) and the audit's
    args: which file, never its content."""
    filename = (file.filename or default_name).replace("\\", "/").rsplit("/", 1)[-1][:200]
    return filename, {
        "connection_id": str(connection_id),
        "filename": filename,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _statement_type(file: UploadFile) -> None:
    """XML by its type, or by its name when the browser sends no type or a generic one."""
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    named_xml = (file.filename or "").lower().endswith(".xml")
    if content_type in XML_TYPES or (named_xml and content_type in ("", GENERIC_TYPE)):
        return
    raise HTTPException(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        {"error": "not_xml", "message": "upload the Flex statement as an XML file"},
    )


@router.post(
    "/{connection_id}/ibkr-statement",
    name="upload_ibkr_statement",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_ibkr_statement(
    connection_id: UUID,
    file: Annotated[UploadFile, File()],
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
    store: DocumentStoreDep,
    session_factory: SessionFactoryDep,
) -> IbkrUploadOut:
    """Ingest a Flex statement the owner downloaded: the closed ISA, which the Flex Web
    Service can't fetch. It goes through the same ingest and normalise as a fetched one."""
    settings = _settings(request)
    connection = await get_connection(session, connection_id)
    if (refusal := _not_connector(connection, "ibkr")) is not None:
        raise refusal
    if not connection.cursor:
        # The first sync (queued when the connection was made) writes the same rows: an
        # upload meanwhile would collide with it.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": "first_sync_pending", "message": "wait for the first sync to finish"},
        )
    _statement_type(file)
    body = await read_upload(file, MAX_IBKR_UPLOAD_BYTES)
    filename, args = _upload_args(connection_id, file, body, "statement.xml")
    try:
        async with healing_command(session, actor, "upload_ibkr_statement", args) as ctx:
            sync_id = (await session.execute(select(func.uuidv7()))).scalar_one()
            sync_ctx = SyncContext(
                command=ctx,
                connection=connection,
                sync_id=sync_id,
                master_key=_require_master_key(settings),
                http=http,
                notifier=notifier_from_settings(settings, http),
                session_factory=session_factory,
                documents=store,
            )
            result = await ingest_statement(sync_ctx, body, {"upload": filename})
            await normalize(sync_ctx, result.raw_record_ids, result.flagged, result.sections)
    except FlexDataError as exc:
        # The message names a section and field only, never a value.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"error": "statement_invalid", "message": str(exc)},
        ) from exc
    return IbkrUploadOut(raw_records=len(result.raw_record_ids))


# eToro (design §5.1, §5.3, §5.7, §5.14) ---------------------------------------------------


async def _etoro_user(http: httpx.AsyncClient, body: EtoroConnectionUpdate) -> tuple[str, str]:
    """The key's user (gcid, realCid), from one `/me` call, and the read-only gate (Decision
    13): a key that can trade or move money is refused before anything is written. Every
    message is fixed text: no scope, nothing the answer or the request holds."""
    client = EtoroClient(
        http,
        Secret(body.public_api_key.get_secret_value()),
        Secret(body.user_key.get_secret_value()),
    )
    try:
        me = await client.me()
    except ReadOnlyGateError as error:
        # Its message is one of the client's fixed texts (no scopes, or not read-only).
        raise _unprocessable("key_not_read_only", str(error)) from None
    except HttpAuthError:
        raise _unprocessable("key_rejected", "eToro rejected this key") from None
    except HttpError:
        raise _unprocessable(
            "key_not_checked",
            "eToro could not check this key: eToro must be reachable, and its firewall must"
            " accept this server. Try again.",
        ) from None
    if not client.cleared:
        # me() returned the answer, but its scopes are not all read-only.
        raise _unprocessable("key_not_read_only", GATE_MESSAGE)
    try:
        return me_ids(me)
    except EtoroSyncError as error:
        # Names the endpoint and field only.
        raise _unprocessable("no_user_id", str(error)) from None


@router.post("/etoro", name="create_etoro_connection", status_code=status.HTTP_201_CREATED)
async def create_etoro_connection(
    body: EtoroConnectionIn,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
) -> ConnectionOut:
    settings = _settings(request)
    _require_master_key(settings)
    gcid, real_cid = await _etoro_user(http, body)
    return await _create_with_keys(
        session,
        actor,
        settings,
        connector="etoro",
        args=body.model_dump(mode="json", exclude={"public_api_key", "user_key"}),
        display_name=body.display_name,
        config={},
        keys=_etoro_keys(body),
        cursor={"gcid": gcid, "real_cid": real_cid},
        identity="real_cid",
        taken="this eToro user is already connected",
    )


@router.put("/{connection_id}/etoro", name="update_etoro_connection")
async def update_etoro_connection(
    connection_id: UUID,
    body: EtoroConnectionUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
) -> ConnectionOut:
    """Replace the two keys. The sync's gate checks that they are read-only and belong to
    the same user; a disabled connection is checked first, as a create is (the read-only
    gate), and is healthy again."""

    async def check() -> dict[str, JSONValue]:
        gcid, real_cid = await _etoro_user(http, body)
        return {"gcid": gcid, "real_cid": real_cid}

    return await _update_keys(
        session,
        actor,
        _settings(request),
        connection_id,
        "etoro",
        _etoro_keys(body),
        check=check,
        identity="real_cid",
        other="these keys are another eToro user's",
    )


async def _lock_for_normalizing(ctx: CommandContext, connection_id: UUID) -> ConnectionRecord:
    """Lock waits bounded by LOCK_TIMEOUT; the connection's row first, as a sync locks it
    before it normalizes, then the matching lock, before the command locks any other row (the
    order healing_command keeps), so heal_transfers at the end waits on nothing."""
    await ctx.session.execute(select(func.set_config("lock_timeout", LOCK_TIMEOUT, True)))
    connection = await lock_connection(ctx, connection_id)
    await transfers.lock_transfers(ctx)
    return connection


def _upload_sync_context(
    ctx: CommandContext,
    connection: ConnectionRecord,
    sync_id: UUID,
    settings: Settings,
    http: httpx.AsyncClient,
    session_factory: SessionFactoryDep,
) -> SyncContext:
    return SyncContext(
        command=ctx,
        connection=connection,
        sync_id=sync_id,
        master_key=_require_master_key(settings),
        http=http,
        notifier=notifier_from_settings(settings, http),
        session_factory=session_factory,
    )


def _no_upload(connection: ConnectionRecord, connector: str, upload: str) -> HTTPException | None:
    """422 unless the connection is the connector's and not disabled."""
    if (refusal := _not_connector(connection, connector)) is not None:
        return refusal
    if connection.status == "disabled":
        return _unprocessable("not_updatable", f"a disabled connection can't take {upload}")
    return None


@router.post(
    "/{connection_id}/etoro-statement",
    name="upload_etoro_statement",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_etoro_statement(
    connection_id: UUID,
    file: Annotated[UploadFile, File()],
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
    store: DocumentStoreDep,
    session_factory: SessionFactoryDep,
) -> EtoroUploadOut:
    """Ingest an eToro account statement (xlsx) the owner downloaded, normalise it with the
    API's records and heal the transfers, in one command (design §5.3). Its file is written
    last, just before the commit: a refused or failed upload leaves none behind."""
    settings = _settings(request)
    connection = await get_connection(session, connection_id)
    if (refusal := _no_upload(connection, "etoro", "a statement")) is not None:
        raise refusal
    body = await read_upload(file, MAX_ETORO_UPLOAD_BYTES)
    filename, args = _upload_args(connection_id, file, body, "statement.xlsx")
    try:
        # Read before any lock is taken: a large file holds no one up.
        statement = await parse_etoro_upload(body)
        async with command_context(session, actor, "upload_etoro_statement", args) as ctx:
            connection = await _lock_for_normalizing(ctx, connection_id)
            if (refusal := _no_upload(connection, "etoro", "a statement")) is not None:
                raise refusal
            sync_id = (await session.execute(select(func.uuidv7()))).scalar_one()
            sync_ctx = _upload_sync_context(
                ctx, connection, sync_id, settings, http, session_factory
            )
            result = await ingest_etoro_statement(sync_ctx, body, filename, statement)
            await transfers.heal_transfers(ctx)
            # The bytes last, once nothing in the command can fail but its commit.
            store.put(result.document_sha256, body)
    except StatementTooEarly as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"error": "first_sync_pending", "message": str(error)}
        ) from None
    except (StatementRejected, StatementError, XlsxError, EtoroDataError) as error:
        # The message names a sheet, a column, a rule or a masked position id, never a
        # value (design §5.3, Decision 9).
        raise _unprocessable("statement_invalid", str(error)) from None
    return EtoroUploadOut(raw_records=len(result.raw_record_ids))


# Trading 212's optional export (design §5.3a) ------------------------------------------------


def _no_export(connection: ConnectionRecord) -> HTTPException | None:
    return _no_upload(connection, "trading212", "an export")


@router.post(
    "/{connection_id}/trading212-export",
    name="upload_trading212_export",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_trading212_export(
    connection_id: UUID,
    file: Annotated[UploadFile, File()],
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
    store: DocumentStoreDep,
    session_factory: SessionFactoryDep,
) -> Trading212UploadOut:
    """Ingest a Trading 212 CSV export the owner downloaded, normalise it with the API's
    records and heal the transfers, in one command. Optional: it adds what the API lacks.
    Its file is written last, just before the commit: a refused or failed upload leaves none
    behind."""
    settings = _settings(request)
    connection = await get_connection(session, connection_id)
    if (refusal := _no_export(connection)) is not None:
        raise refusal
    body = await read_upload(file, MAX_TRADING212_UPLOAD_BYTES)
    filename, args = _upload_args(connection_id, file, body, "export.csv")
    try:
        # Read before any lock is taken: a large file holds no one up.
        parsed = await parse_trading212_upload(body)
        async with command_context(session, actor, "upload_trading212_export", args) as ctx:
            connection = await _lock_for_normalizing(ctx, connection_id)
            if (refusal := _no_export(connection)) is not None:
                raise refusal
            sync_id = (await session.execute(select(func.uuidv7()))).scalar_one()
            sync_ctx = _upload_sync_context(
                ctx, connection, sync_id, settings, http, session_factory
            )
            result = await ingest_trading212_export(sync_ctx, body, filename, parsed)
            await transfers.heal_transfers(ctx)
            # The bytes last, once nothing in the command can fail but its commit.
            store.put(result.document_sha256, body)
    except ExportTooEarly as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"error": "first_sync_pending", "message": str(error)}
        ) from None
    except (ExportError, Trading212DataError) as error:
        # The message names a column, a field or a rule, never a value.
        raise _unprocessable("export_invalid", str(error)) from None
    return Trading212UploadOut(raw_records=len(result.raw_record_ids))


# Review items (design §5.7): eToro's API and statement, Trading 212's API and export ---------

# The connectors with two sources of an event; each one's normalizer posts a decision.
_REVIEWED = ("etoro", "trading212")


def _no_reviews(connection: ConnectionRecord) -> HTTPException | None:
    """422 unless the connection's connector has review items."""
    if connection.connector not in _REVIEWED:
        return _unprocessable(
            "no_reviews", "only eToro and Trading 212 connections have review items"
        )
    return None


@router.get("/{connection_id}/reviews", name="list_source_reviews")
async def list_source_reviews(connection_id: UUID, session: SessionDep) -> list[ReviewOut]:
    """The connection's review items (design §5.7): open ones first, newest first. Only an
    eToro or a Trading 212 connection has them."""
    connection = await get_connection(session, connection_id)
    if (refusal := _no_reviews(connection)) is not None:
        raise refusal
    return await list_reviews(session, connection_id)


@router.post(
    "/{connection_id}/reviews/{review_id}/resolve",
    name="resolve_source_review",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def resolve_source_review(
    connection_id: UUID,
    review_id: UUID,
    body: ReviewResolve,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
    session_factory: SessionFactoryDep,
) -> None:
    """Take one source's value, recorded as the owner's decision, and normalise at once so the
    decision posts now, then heal the transfers the new postings touch."""
    settings = _settings(request)
    # Before anything else: only an eToro or a Trading 212 connection has review items.
    found = await get_connection(session, connection_id)
    if (refusal := _no_reviews(found)) is not None:
        raise refusal
    args: dict[str, JSONValue] = {
        "connection_id": str(connection_id),
        "review_id": str(review_id),
        "take": body.take,
    }
    async with command_context(session, actor, "resolve_source_review", args) as ctx:
        connection = await _lock_for_normalizing(ctx, connection_id)
        # A disabled connection normalizes nothing, as its uploads refuse.
        if connection.status == "disabled":
            raise _unprocessable("not_updatable", "a disabled connection can't take a decision")
        owner = await session.scalar(
            select(models.SourceReview.connection_id).where(models.SourceReview.id == review_id)
        )
        if owner != connection_id:
            # Another connection's item is not this one's to resolve.
            raise ReviewNotFoundError(str(review_id))
        await resolve_review(ctx, review_id, body.take)
        sync_id = (await session.execute(select(func.uuidv7()))).scalar_one()
        sync_ctx = _upload_sync_context(ctx, connection, sync_id, settings, http, session_factory)
        try:
            # Nothing new to apply: each run applies the events decided on.
            if connection.connector == "trading212":
                await trading212_publish_replayed(
                    sync_ctx, await trading212_normalize(sync_ctx, [])
                )
            else:
                await etoro_publish_replayed(sync_ctx, await etoro_normalize(sync_ctx, []))
        except (EtoroDataError, Trading212DataError) as error:
            # What is stored cannot be posted; the message names no value.
            raise _unprocessable("not_postable", str(error)) from None
        await transfers.heal_transfers(ctx)
