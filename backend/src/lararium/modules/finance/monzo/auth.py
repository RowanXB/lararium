"""Monzo OAuth: the authorisation URL, token exchange and refresh, and token storage.

Refresh tokens are single-use: each refresh returns a new pair and invalidates the old one.
So a new pair is committed in its own short transaction before it is used (Decision 4);
if anything later fails, the next run still holds the valid refresh token.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import load_credential, load_credential_expiry, store_credential
from lararium.core.connectors import SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.secrets import Secret
from lararium.modules.finance.monzo.client import (
    API,
    AUTH,
    MonzoApiError,
    error_code,
    raise_for_error,
)

TOKEN_URL = f"{API}/oauth2/token"
# Refresh when the access token has less than this left, so it can't expire mid-sync.
REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class TokenPair:
    access_token: Secret
    refresh_token: Secret
    expires_at: datetime


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{AUTH}?{query}"


async def _token_request(http: httpx.AsyncClient, form: dict[str, str]) -> TokenPair:
    response = await http.post(TOKEN_URL, data=form)
    # Any client error but a rate limit means these credentials won't work again: an expired
    # or reused refresh token comes back as 400 or 401, under several codes.
    if 400 <= response.status_code < 500 and response.status_code != 429:
        raise CredentialRejectedError(
            f"Monzo rejected the credentials ({error_code(response)}); authorise again"
        )
    raise_for_error(response)
    body = response.json()
    if not body.get("refresh_token"):
        # Only confidential clients get refresh tokens.
        raise MonzoApiError(response.status_code, "missing_refresh_token")
    return TokenPair(
        access_token=Secret(body["access_token"]),
        refresh_token=Secret(body["refresh_token"]),
        expires_at=datetime.now(UTC) + timedelta(seconds=int(body["expires_in"])),
    )


async def exchange_code(
    http: httpx.AsyncClient, client_id: str, client_secret: Secret, redirect_uri: str, code: str
) -> TokenPair:
    return await _token_request(
        http,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret.reveal(),
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )


async def refresh(
    http: httpx.AsyncClient, client_id: str, client_secret: Secret, refresh_token: Secret
) -> TokenPair:
    return await _token_request(
        http,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret.reveal(),
            "refresh_token": refresh_token.reveal(),
        },
    )


async def store_tokens(
    ctx: CommandContext, master_key: bytes, connection_id: UUID, pair: TokenPair
) -> None:
    """Encrypt and store the pair in the command's transaction."""
    await store_credential(
        ctx, master_key, connection_id, "access_token", pair.access_token.reveal(), pair.expires_at
    )
    await store_credential(
        ctx, master_key, connection_id, "refresh_token", pair.refresh_token.reveal(), None
    )


async def save_tokens(
    session_factory: async_sessionmaker[AsyncSession],
    master_key: bytes,
    connection_id: UUID,
    pair: TokenPair,
    *,
    actor: Actor,
) -> None:
    """Encrypt and commit the pair in a session and transaction of its own."""
    args = {"connection_id": str(connection_id)}
    async with (
        session_factory() as session,
        command_context(session, actor, "connection.save_tokens", args) as ctx,
    ):
        await store_tokens(ctx, master_key, connection_id, pair)


async def access_token_for(sync_ctx: SyncContext, *, force_refresh: bool = False) -> Secret:
    """A usable access token, refreshed (and committed) first when under 5 minutes remain, or
    when `force_refresh` (Monzo rejected the stored one before its expiry)."""
    session = sync_ctx.command.session
    connection = sync_ctx.connection
    key = sync_ctx.master_key
    access = await load_credential(session, key, connection.id, "access_token")
    expires_at = await load_credential_expiry(session, connection.id, "access_token")
    if (
        not force_refresh
        and access is not None
        and expires_at is not None
        and expires_at - datetime.now(UTC) >= REFRESH_MARGIN
    ):
        return access

    client_id = connection.config.get("client_id")
    client_secret = await load_credential(session, key, connection.id, "client_secret")
    refresh_token = await load_credential(session, key, connection.id, "refresh_token")
    if not isinstance(client_id, str) or client_secret is None or refresh_token is None:
        raise CredentialRejectedError(
            "the Monzo connection has no usable credentials; authorise it"
        )
    pair = await refresh(sync_ctx.http, client_id, client_secret, refresh_token)
    await save_tokens(
        sync_ctx.session_factory, key, connection.id, pair, actor=sync_ctx.command.actor
    )
    return pair.access_token
