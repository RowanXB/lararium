"""/api/v1/connections routes and the Monzo OAuth flow (Decision 7).

Each route's name is its OpenAPI operationId. Credentials (the client secret, tokens) and
the OAuth code and state never reach a response, the audit log or the logs.
"""

import hashlib
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal, cast
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
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.config import Settings, load_master_key
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    AUTHORISED_AT_KEY,
    ConnectionRecord,
    ConnectionStatus,
    consume_oauth_state,
    create_connection,
    delete_credential,
    get_connection,
    list_connections,
    load_credential,
    lock_connection,
    request_sync,
    set_credential_expiry,
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
from lararium.core.models import Connection
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
from lararium.modules.finance.api import read_upload
from lararium.modules.finance.binance.accounts import ACCOUNT_MAP_KEY, wallet_name
from lararium.modules.finance.binance.balances import read_balances
from lararium.modules.finance.binance.client import BinanceError
from lararium.modules.finance.binance.normalize import FLAGS_KEY
from lararium.modules.finance.binance.reconcile import open_differences
from lararium.modules.finance.binance.sync import (
    ACCOUNT_LABEL,
    FIRST_SYNC_DONE_KEY,
    make_client,
    pass_gate,
    uid_of,
)
from lararium.modules.finance.ibkr.normalize import normalize
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.sync import (
    EXPIRY_NOTIFIED_KEY,
    LONDON,
    TOKEN_PURPOSE,
    ingest_statement,
)
from lararium.modules.finance.models import Instrument
from lararium.modules.finance.monzo.auth import authorize_url, exchange_code, store_tokens
from lararium.modules.finance.monzo.client import MonzoApiError
from lararium.modules.finance.transfers_api import healing_command
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
    # A Binance connection's own state; None for every other connector.
    binance: "BinanceConnectionState | None" = None


class BinanceDifferenceOut(BaseModel):
    """A reconciliation difference that still holds: where and when, never an amount."""

    account_id: UUID
    symbol: str
    from_at: datetime
    to_at: datetime


class BinanceConnectionState(BaseModel):
    # The display names of the wallets found so far.
    wallets: list[str]
    first_sync_done: bool
    differences: list[BinanceDifferenceOut]
    flags: list[str]


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


class _BinanceKey(BaseModel):
    """The key material. Checked in the routes, not by validators or field limits: a pydantic
    error would repeat the input, secrets included, in its response."""

    key_type: Literal["hmac", "ed25519"]
    api_key: SecretStr
    # hmac: the secret. ed25519: the private key (PEM) and its passphrase, if it has one.
    api_secret: SecretStr | None = None
    private_key: SecretStr | None = None
    private_key_passphrase: SecretStr | None = None


class BinanceConnectionCreate(_BinanceKey):
    display_name: str = Field(min_length=1, max_length=200)


class BinanceConnectionUpdate(_BinanceKey):
    pass


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
    app.add_exception_handler(RequestValidationError, _validation_error)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's answer, except that a Binance key's route never repeats the request: an error
    of a missing field carries the whole body, secrets included."""
    assert isinstance(exc, RequestValidationError)
    if not request.url.path.endswith("/binance"):
        return await request_validation_exception_handler(request, exc)
    errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
    return JSONResponse({"detail": jsonable_encoder(errors)}, status.HTTP_422_UNPROCESSABLE_CONTENT)


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


async def _binance_state(session: AsyncSession, record: ConnectionRecord) -> BinanceConnectionState:
    mapped = record.config.get(ACCOUNT_MAP_KEY)
    names: list[str] = []
    for key in mapped if isinstance(mapped, dict) else {}:
        try:
            names.append(wallet_name(key))
        except ValueError:
            continue
    held = await open_differences(session, record.id)
    shown = await session.execute(
        select(Instrument.id, Instrument.symbol).where(
            Instrument.id.in_({d.instrument_id for d in held})
        )
    )
    symbols = {row.id: row.symbol for row in shown}
    flags = record.config.get(FLAGS_KEY)
    return BinanceConnectionState(
        wallets=sorted(names),
        first_sync_done=record.config.get(FIRST_SYNC_DONE_KEY) is True,
        differences=[
            BinanceDifferenceOut(
                account_id=d.account_id,
                symbol=symbols[d.instrument_id],
                from_at=d.from_at,
                to_at=d.to_at,
            )
            for d in held
        ],
        flags=[f for f in flags if isinstance(f, str)] if isinstance(flags, list) else [],
    )


async def _out_with_state(
    session: AsyncSession, record: ConnectionRecord, settings: Settings
) -> ConnectionOut:
    out = _out(record, settings)
    if record.connector == "binance":
        out.binance = await _binance_state(session, record)
    return out


@router.get("", name="list_connections")
async def list_connections_route(request: Request, session: SessionDep) -> list[ConnectionOut]:
    settings = _settings(request)
    return [
        await _out_with_state(session, record, settings)
        for record in await list_connections(session)
    ]


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
async def sync_connection_now(
    connection_id: UUID, session: SessionDep, actor: ActorDep
) -> SyncQueuedOut:
    # The request is recorded before the job is queued, so whichever sync job runs next,
    # this one or one already queued, sees it.
    args = {"connection_id": str(connection_id)}
    async with command_context(session, actor, "sync_connection_now", args) as ctx:
        await request_sync(ctx, connection_id)
    return SyncQueuedOut(queued=await defer_sync(connection_id))


@router.post("/{connection_id}/disable", name="disable_connection")
async def disable_connection(
    connection_id: UUID, request: Request, session: SessionDep, actor: ActorDep
) -> ConnectionOut:
    args = {"connection_id": str(connection_id)}
    async with command_context(session, actor, "disable_connection", args) as ctx:
        record = await set_status(ctx, connection_id, "disabled")
    return await _out_with_state(session, record, _settings(request))


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


def _not_ibkr(connection: ConnectionRecord) -> HTTPException | None:
    if connection.connector != "ibkr":
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"error": "not_ibkr", "message": "this is not an IBKR connection"},
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
        if (refusal := _not_ibkr(connection)) is not None:
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


# Binance (design 5.1 and 5.15)

MAX_KEY_CHARS = 10_000
# Serialises creates, so two that name one account can't both pass the check for it.
BINANCE_CREATE_LOCK = 7_104_001


def _invalid(error: str, message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, {"error": error, "message": message}
    )


def _material(body: _BinanceKey) -> dict[str, Secret]:
    """The credentials to store, by purpose, once the key type's rules hold."""
    given = {
        "api_key": body.api_key,
        "api_secret": body.api_secret,
        "private_key": body.private_key,
        "private_key_passphrase": body.private_key_passphrase,
    }
    material: dict[str, Secret] = {}
    for purpose, value in given.items():
        if value is None or (purpose == "private_key_passphrase" and not value.get_secret_value()):
            continue
        revealed = value.get_secret_value()
        if not revealed or len(revealed) > MAX_KEY_CHARS:
            raise _invalid("key_invalid", f"{purpose} must be 1 to {MAX_KEY_CHARS} characters")
        material[purpose] = Secret(revealed)
    if "api_key" not in material:
        raise _invalid("key_invalid", "api_key is required")
    if body.key_type == "hmac":
        if (
            "api_secret" not in material
            or "private_key" in material
            or "private_key_passphrase" in material
        ):
            raise _invalid(
                "key_invalid",
                "an HMAC key needs api_secret, and takes no private_key or passphrase",
            )
    elif "private_key" not in material or "api_secret" in material:
        raise _invalid(
            "key_invalid",
            "an Ed25519 key needs private_key (and its passphrase, if it has one),"
            " and takes no api_secret",
        )
    return material


def _unreachable(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, {"error": "binance_unavailable", "message": message}
    )


async def _checked_uid(
    http: httpx.AsyncClient, body: _BinanceKey, material: dict[str, Secret]
) -> str:
    """Ask Binance, with nothing stored: the gate first (nothing else is sent with a key it
    refuses), then the account the key belongs to."""
    try:
        client = make_client(
            http,
            material["api_key"],
            body.key_type,
            api_secret=material.get("api_secret"),
            private_key=material.get("private_key"),
            passphrase=material.get("private_key_passphrase"),
        )
        await pass_gate(client)
        uid = uid_of((await read_balances(client, [ACCOUNT_LABEL]))[ACCOUNT_LABEL])
    except CredentialRejectedError as exc:
        # Written to be shown: a permission's name or a code, never the key.
        raise _invalid("key_rejected", str(exc)) from None
    except BinanceError as exc:
        raise _unreachable(f"{exc}; nothing was saved") from None
    except httpx.HTTPError as exc:
        logger.warning("Binance key check failed: %s", type(exc).__name__)
        raise _unreachable("Binance can't be reached; nothing was saved") from None
    if uid is None:
        raise _invalid("key_rejected", "Binance did not say which account this key belongs to")
    return uid


async def _store_key(
    ctx: Any, master_key: bytes, connection_id: UUID, material: dict[str, Secret]
) -> None:
    """Store the key's parts and drop the others (a switch of key type leaves none behind)."""
    for purpose in ("api_key", "api_secret", "private_key", "private_key_passphrase"):
        if purpose in material:
            await store_credential(
                ctx, master_key, connection_id, purpose, material[purpose].reveal(), None
            )
        else:
            await delete_credential(ctx, connection_id, purpose)


@router.post("/binance", name="create_binance_connection", status_code=status.HTTP_201_CREATED)
async def create_binance_connection(
    body: BinanceConnectionCreate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
) -> ConnectionOut:
    settings = _settings(request)
    master_key = _require_master_key(settings)
    material = _material(body)
    uid = await _checked_uid(http, body, material)
    args = body.model_dump(mode="json", include={"display_name", "key_type"})
    async with command_context(session, actor, "create_binance_connection", args) as ctx:
        await session.execute(select(func.pg_advisory_xact_lock(BINANCE_CREATE_LOCK)))
        taken = await session.scalar(
            select(Connection.id).where(
                Connection.connector == "binance", Connection.config["uid"].as_string() == uid
            )
        )
        if taken is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "error": "already_connected",
                    "message": "this Binance account is already connected",
                },
            )
        created = await create_connection(
            ctx,
            connector="binance",
            display_name=body.display_name,
            config={"key_type": body.key_type, "uid": uid},
        )
        await _store_key(ctx, master_key, created.id, material)
        # There is no approval step: a key that passed the gate works at once.
        record = await set_status(ctx, created.id, "healthy")
    # After the commit, so the job finds the connection.
    await defer_sync(record.id)
    return await _out_with_state(session, record, settings)


def _require_binance_updatable(connection: ConnectionRecord) -> None:
    if connection.connector != "binance":
        raise _invalid("not_binance", "this is not a Binance connection")
    if connection.status == "disabled":
        raise _invalid("not_updatable", "a disabled connection can't be updated")


@router.put("/{connection_id}/binance", name="update_binance_connection")
async def update_binance_connection(
    connection_id: UUID,
    body: BinanceConnectionUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    http: HttpDep,
) -> ConnectionOut:
    """Replace the key (a new one after the old was rejected). It must belong to the account
    already connected."""
    settings = _settings(request)
    master_key = _require_master_key(settings)
    material = _material(body)
    _require_binance_updatable(await get_connection(session, connection_id))
    uid = await _checked_uid(http, body, material)
    args = body.model_dump(mode="json", include={"key_type"}) | {
        "connection_id": str(connection_id)
    }
    async with command_context(session, actor, "update_binance_connection", args) as ctx:
        # Checked under the row lock: a disable made meanwhile can't slip in before the write.
        connection = await lock_connection(ctx, connection_id)
        _require_binance_updatable(connection)
        if uid != connection.config.get("uid"):
            raise _invalid(
                "another_account",
                "this key belongs to another Binance account than this connection",
            )
        await _store_key(ctx, master_key, connection_id, material)
        await update_config(ctx, connection_id, {"key_type": body.key_type})
        await transition_status(
            ctx, connection_id, "healthy", only_from=("needs_reauth", "failing"), clear_error=True
        )
        record = await get_connection(session, connection_id)
    await defer_sync(connection_id)
    return await _out_with_state(session, record, settings)


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
    if (refusal := _not_ibkr(connection)) is not None:
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
    filename = (file.filename or "statement.xml").replace("\\", "/").rsplit("/", 1)[-1][:200]
    # The audit records which file, never its content.
    args: dict[str, JSONValue] = {
        "connection_id": str(connection_id),
        "filename": filename,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
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
