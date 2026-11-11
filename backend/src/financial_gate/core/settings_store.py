from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from financial_gate.core.commands import CommandContext
from financial_gate.core.errors import SettingNotFoundError
from financial_gate.core.json_types import JSONValue
from financial_gate.core.models import Setting

TIMEZONE_KEY = "core.timezone"


async def get_setting(session: AsyncSession, key: str) -> JSONValue:
    # Select the column, not the object, so a stale identity map can't hide an upsert.
    row = (await session.execute(select(Setting.value).where(Setting.key == key))).one_or_none()
    if row is None:
        raise SettingNotFoundError(key)
    return row.value


async def set_setting(ctx: CommandContext, key: str, value: JSONValue) -> None:
    statement = insert(Setting).values(key=key, value=value)
    statement = statement.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": statement.excluded.value, "updated_at": func.now()},
    )
    await ctx.session.execute(statement)
    ctx.record_change(f"setting:{key}", value)


async def get_timezone(session: AsyncSession) -> ZoneInfo:
    value = await get_setting(session, TIMEZONE_KEY)
    if not isinstance(value, str):
        raise ValueError(f"{TIMEZONE_KEY} must be a string, got {value!r}")
    return ZoneInfo(value)
