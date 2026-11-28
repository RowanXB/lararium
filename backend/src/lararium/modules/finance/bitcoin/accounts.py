"""The finance account of a Bitcoin wallet connection (design 5.4): one per connection.

Made the first time a sync needs it and recorded in the connection's `config["account_id"]`.
It opens on the day of the wallet's first confirmed transaction as known then, or, made while
the wallet had none, on that day once a later sync finds one; the chain holds the whole
history, so there is no opening adjustment and nothing moves a date once set.
"""

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import command_context
from lararium.core.connections import get_connection, lock_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.modules.finance.accounts import (
    AccountRecord,
    create_account,
    get_account,
    set_opened_on_if_unknown,
)
from lararium.modules.finance.errors import AccountNotFoundError

ACCOUNT_KEY = "account_id"
INSTITUTION = "Bitcoin"
TYPE_KEY = "bitcoin_wallet"


async def _recorded(session: AsyncSession, config: dict[str, JSONValue]) -> AccountRecord | None:
    found = config.get(ACCOUNT_KEY)
    if not isinstance(found, str):
        return None
    try:
        return await get_account(session, UUID(found))
    except (AccountNotFoundError, ValueError):
        return None  # the account is gone: make it again


async def wallet_account(ctx: SyncContext, opened_on: date | None) -> UUID:
    """The connection's account, made (opening on `opened_on`) if it has none.

    Commits on its own through `ctx.session_factory`, so the identifiers registered next can
    name it in transactions of their own. An existing account is returned as it is, except
    that one without an open date gets `opened_on` (module docstring).
    """
    connection_id = ctx.connection.id
    args: dict[str, JSONValue] = {"connection_id": str(connection_id)}
    async with ctx.session_factory() as session:
        found = await _recorded(session, (await get_connection(session, connection_id)).config)
    if found is not None:
        if found.opened_on is None and opened_on is not None:
            async with (
                ctx.session_factory() as session,
                command_context(session, ctx.command.actor, "bitcoin.account", args) as cmd,
            ):
                await set_opened_on_if_unknown(cmd, found.id, opened_on)
        return found.id
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "bitcoin.account", args) as cmd,
    ):
        # Under the row lock, so two syncs never both make one.
        config = (await lock_connection(cmd, connection_id)).config
        found = await _recorded(session, config)
        if found is not None:
            return found.id
        created = await create_account(
            cmd,
            name=ctx.connection.display_name,
            ledger_type="asset",
            subtype="wallet",
            balance_mode="transactions",
            institution_entity_id=await get_or_create_organization(cmd, INSTITUTION),
            opened_on=opened_on,
            source_connection_id=connection_id,
            type_key=TYPE_KEY,
        )
        await update_config(cmd, connection_id, {ACCOUNT_KEY: str(created.id)})
        return created.id
