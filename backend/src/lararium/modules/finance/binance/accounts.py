"""The finance account of each Binance wallet (design 5.4, plan Decision 8).

A wallet's account is made the first time something needs it and recorded in the connection's
`config["account_map"]` (wallet key to account id). The owner may rename it.
"""

from uuid import UUID

from lararium.core.commands import CommandContext
from lararium.core.connections import ConnectionRecord, get_connection, update_config
from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.modules.finance.accounts import create_account, get_account
from lararium.modules.finance.binance.catalogue import WALLETS, wallet_account_name
from lararium.modules.finance.errors import AccountNotFoundError

ACCOUNT_MAP_KEY = "account_map"
INSTITUTION = "Binance"
# The debt of Crypto Loans is a liability; every other wallet holds assets.
_LIABILITY_WALLETS = frozenset({"loans"})
_OTHER = "other:"
_NAMES = {key: name for name, key in WALLETS.items()}


def wallet_name(wallet: str) -> str:
    """Binance's name for a wallet key (`um_futures` is `USDⓈ-M Futures`)."""
    if wallet.startswith(_OTHER):
        return wallet.removeprefix(_OTHER)
    if wallet not in _NAMES:
        raise ValueError(f"not a wallet key: {wallet}")
    return _NAMES[wallet]


async def wallet_account(ctx: CommandContext, connection: ConnectionRecord, wallet: str) -> UUID:
    """The account of a wallet (a key from `wallet_key`), made if the connection has none.

    Reads the connection's config as it is now, not the record's copy, and adds the account to
    `account_map` without touching any other config key. The institution is looked up under a
    lock held to the end of the transaction, so call this in a short one.
    """
    name = wallet_account_name(wallet_name(wallet))
    current = (await get_connection(ctx.session, connection.id)).config.get(ACCOUNT_MAP_KEY)
    mapped: dict[str, JSONValue] = dict(current) if isinstance(current, dict) else {}
    found = mapped.get(wallet)
    if isinstance(found, str):
        try:
            return (await get_account(ctx.session, UUID(found))).id
        except (AccountNotFoundError, ValueError):
            pass  # the account is gone: make it again
    liability = wallet in _LIABILITY_WALLETS
    created = await create_account(
        ctx,
        name=name,
        ledger_type="liability" if liability else "asset",
        subtype="other" if liability else "exchange",
        balance_mode="hybrid",
        institution_entity_id=await get_or_create_organization(ctx, INSTITUTION),
        source_connection_id=connection.id,
    )
    mapped[wallet] = str(created.id)
    await update_config(ctx, connection.id, {ACCOUNT_MAP_KEY: mapped})
    return created.id
