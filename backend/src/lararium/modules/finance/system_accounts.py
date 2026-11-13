from typing import Literal
from uuid import UUID

from lararium.core.commands import CommandContext
from lararium.core.errors import SettingNotFoundError
from lararium.core.settings_store import get_setting, set_setting
from lararium.modules.finance.accounts import create_account

SystemAccountKey = Literal["unassigned", "opening_balances"]

_NAMES: dict[SystemAccountKey, str] = {
    "unassigned": "Unassigned",
    "opening_balances": "Opening balances",
}


async def get_or_create_system_account(ctx: CommandContext, key: SystemAccountKey) -> UUID:
    """The equity account standing in for the unknown side of a connector's transactions.
    Equity accounts are outside net worth. Its id is remembered in finance.system_account.<key>."""
    setting_key = f"finance.system_account.{key}"
    try:
        value = await get_setting(ctx.session, setting_key)
    except SettingNotFoundError:
        pass
    else:
        if not isinstance(value, str):
            raise ValueError(f"{setting_key} must be an account id string, got {value!r}")
        return UUID(value)
    account = await create_account(
        ctx,
        name=_NAMES[key],
        ledger_type="equity",
        subtype="other",
        balance_mode="transactions",
    )
    await set_setting(ctx, setting_key, str(account.id))
    return account.id
