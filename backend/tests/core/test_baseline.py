import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_schemas_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        text("select nspname from pg_namespace where nspname in ('core', 'finance') order by 1")
    )
    assert rows.scalars().all() == ["core", "finance"]


async def test_backend_role_cannot_create_tables(session: AsyncSession) -> None:
    with pytest.raises(exc.ProgrammingError, match="permission denied"):
        await session.execute(text("create table finance.should_fail (id int)"))


async def test_no_float_columns(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "select table_schema || '.' || table_name || '.' || column_name"
            " from information_schema.columns"
            " where table_schema in ('core', 'finance')"
            " and data_type in ('real', 'double precision')"
        )
    )
    assert rows.scalars().all() == []


async def test_default_timezone_setting(session: AsyncSession) -> None:
    value = await session.scalar(
        text("select value from core.settings where key = 'core.timezone'")
    )
    assert value == "Europe/London"


async def test_exactly_one_self_entity(session: AsyncSession) -> None:
    rows = await session.execute(
        text("select kind, owner_entity_id = id from core.entities where is_self")
    )
    assert rows.all() == [("person", True)]


async def test_backend_role_cannot_modify_audit_log(session: AsyncSession) -> None:
    with pytest.raises(exc.ProgrammingError, match="permission denied"):
        await session.execute(text("update core.audit_log set actor_id = 'x'"))
