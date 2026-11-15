from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.errors import SettingNotFoundError
from lararium.core.models import Setting
from lararium.core.settings_store import get_setting, set_setting
from lararium.modules.finance.accounts import (
    AccountSubtype,
    BalanceMode,
    LedgerType,
    create_account,
)

SystemAccountKey = Literal["unassigned", "opening_balances", "in_transit"]

# Name, ledger type, subtype and balance mode of each.
_ACCOUNTS: dict[SystemAccountKey, tuple[str, LedgerType, AccountSubtype, BalanceMode]] = {
    "unassigned": ("Unassigned", "equity", "other", "transactions"),
    "opening_balances": ("Opening balances", "equity", "other", "transactions"),
    # Money on its way between two own accounts: an asset, so it stays in net worth.
    "in_transit": ("In transit", "asset", "in_transit", "transactions"),
}


async def get_or_create_system_account(ctx: CommandContext, key: SystemAccountKey) -> UUID:
    """An account the ledger itself keeps: Unassigned and Opening balances, equity accounts
    (outside net worth) standing in for the unknown side of a connector's transactions, and
    In transit, the asset holding a transfer between own accounts until both sides are in.
    Its id is remembered in finance.system_account.<key>.

    Concurrent callers (syncs of different connections) are serialised on the key, so only
    one creates it; call it in a short transaction, as the lock is held until it commits.
    """
    setting_key = f"finance.system_account.{key}"
    await ctx.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(setting_key))))
    try:
        value = await get_setting(ctx.session, setting_key)
    except SettingNotFoundError:
        pass
    else:
        if not isinstance(value, str):
            raise ValueError(f"{setting_key} must be an account id string, got {value!r}")
        return UUID(value)
    name, ledger_type, subtype, balance_mode = _ACCOUNTS[key]
    account = await create_account(
        ctx, name=name, ledger_type=ledger_type, subtype=subtype, balance_mode=balance_mode
    )
    await set_setting(ctx, setting_key, str(account.id))
    return account.id


async def system_account_ids(session: AsyncSession) -> dict[SystemAccountKey, UUID]:
    """The system accounts created so far, by key; reading needs no command."""
    by_setting: dict[str, SystemAccountKey] = {
        f"finance.system_account.{key}": key for key in _ACCOUNTS
    }
    rows = await session.execute(
        select(Setting.key, Setting.value).where(Setting.key.in_(list(by_setting)))
    )
    found: dict[SystemAccountKey, UUID] = {}
    for row in rows:
        if not isinstance(row.value, str):
            raise ValueError(f"{row.key} must be an account id string, got {row.value!r}")
        found[by_setting[row.key]] = UUID(row.value)
    return found
