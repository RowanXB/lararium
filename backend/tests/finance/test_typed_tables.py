from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.db import create_engine
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.tables import parse_date as flex_parse_date
from lararium.modules.finance.typed_tables import (
    TypedValueError,
    column_map,
    column_name,
    parse_date,
    parse_decimal,
    parse_moment,
    typed_values,
    upsert_typed,
)


@pytest.mark.parametrize(
    ("path", "column"),
    [
        ("walletImpact.fxRate", "wallet_impact_fx_rate"),
        ("instrument.isin", "instrument_isin"),
        ("orderID", "order_id"),
        ("Spread Fees (USD)", "spread_fees_usd"),
        ("Long / Short", "long_short"),
        ("FX rate at open (USD)", "fx_rate_at_open_usd"),
    ],
)
def test_column_name_paths(path: str, column: str) -> None:
    assert column_name(path) == column


def test_parse_date_separators_are_the_callers() -> None:
    assert parse_date("2026-11-10 09:30", "d") == date(2026, 11, 10)
    with pytest.raises(TypedValueError, match="not a date"):
        parse_date("20261110 093000", "d", separators=";")
    assert parse_date("20261110;093000", "d", separators=";") == date(2026, 11, 10)


def test_column_map_rejects_colliding_paths() -> None:
    assert column_map(["Profit(USD)", "Long / Short", "Profit(USD)"]) == {
        "Profit(USD)": "profit_usd",
        "Long / Short": "long_short",
    }
    with pytest.raises(
        ValueError, match=r"'Profit\(USD\)' and 'Profit USD' are both column profit_usd"
    ):
        column_map(["Profit(USD)", "Profit USD"])
    with pytest.raises(ValueError, match="no column name"):
        column_map(["()"])


def test_ibkr_dates_keep_their_exact_acceptance() -> None:
    assert flex_parse_date("20261110;093000", "d") == date(2026, 11, 10)
    for loose in ("20261110 093000", "2026-11-10 09:30", "2026-11-10T09:30:00"):
        with pytest.raises(FlexDataError, match=r"^d is not a date$"):
            flex_parse_date(loose, "d")


def test_parsers_name_the_field_not_the_value() -> None:
    assert parse_date("20261110", "d") == date(2026, 11, 10)
    assert parse_date("2026-11-10T09:30:00Z", "d") == date(2026, 11, 10)
    assert parse_moment("2026-11-10T09:30:00Z", "m") == datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
    assert parse_decimal(" 1.50 ", "n") == Decimal("1.50")
    with pytest.raises(TypedValueError, match=r"^d is not a date$"):
        parse_date("secret", "d")
    with pytest.raises(TypedValueError, match=r"^m is not a date-time$"):
        parse_moment("secret", "m")
    with pytest.raises(TypedValueError, match=r"^n is not a number$"):
        parse_decimal("NaN", "n")


@pytest.fixture
async def owner_session(owner_url: str, migrated: None):
    """The schema owner's session: the backend role cannot create the scratch table."""
    engine = create_engine(owner_url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            db_session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield db_session
            finally:
                await db_session.close()
                await outer.rollback()
    finally:
        await engine.dispose()


async def _scratch(session) -> None:
    await session.execute(
        text(
            "create table finance.typed_scratch (id text primary key, n numeric, d date,"
            " at timestamptz, local_at timestamp, flag boolean, note text, ref uuid, count integer)"
        )
    )


async def test_typed_values_casts_to_column_types(owner_session) -> None:
    session = owner_session
    await _scratch(session)
    values = await typed_values(
        session,
        "typed_scratch",
        {
            "id": "a",
            "n": "1.50",
            "d": "2026-11-10",
            "at": "2026-11-10T09:30:00Z",
            "local_at": "2026-11-10T09:30:00",
            "flag": "true",
            "note": "",
            "ref": "ignored",
            "unknown": "x",
        },
    )
    assert values == {
        "id": "a",
        "n": Decimal("1.50"),
        "d": date(2026, 11, 10),
        "at": datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        "local_at": datetime(2026, 11, 10, 9, 30),  # noqa: DTZ001 (zone-less column)
        "flag": True,
        "note": None,
    }
    assert await typed_values(session, "typed_scratch", {"n": Decimal("2"), "flag": False}) == {
        "n": Decimal("2"),
        "flag": False,
    }
    with pytest.raises(TypedValueError, match=r"^Order\.n is not a number$"):
        await typed_values(session, "typed_scratch", {"n": "x"}, {"n": "Order.n"})
    with pytest.raises(TypedValueError, match="no time zone"):
        await typed_values(session, "typed_scratch", {"at": "2026-11-10T09:30:00"})
    with pytest.raises(TypedValueError, match=r"^flag is not true or false$"):
        await typed_values(session, "typed_scratch", {"flag": "maybe"})
    with pytest.raises(TypedValueError, match=r"^local_at has a time zone, its column has none$"):
        await typed_values(session, "typed_scratch", {"local_at": "2026-11-10T09:30:00Z"})
    with pytest.raises(TypedValueError, match=r"^count has a column type"):
        await typed_values(session, "typed_scratch", {"count": "1"})
    with pytest.raises(ValueError, match="no table"):
        await typed_values(session, "no_such_table", {"a": "b"})


async def test_upsert_typed_writes_once(owner_session) -> None:
    session = owner_session
    await _scratch(session)

    async def run(fn, /, *args):
        async with command_context(session, Actor("user", "test"), "test", {}) as ctx:
            return await fn(ctx, *args)

    key = {"id": "a"}
    values = {"n": Decimal("1.5"), "note": "x"}
    assert await run(upsert_typed, "typed_scratch", key, values) is True
    assert await run(upsert_typed, "typed_scratch", key, values) is False
    assert (
        await run(upsert_typed, "typed_scratch", key, {"n": Decimal("1.50"), "note": "x"}) is False
    )
    assert await run(upsert_typed, "typed_scratch", key, {**values, "note": "y"}) is True
    assert await run(upsert_typed, "typed_scratch", {"id": "b"}, {}) is True
    assert await run(upsert_typed, "typed_scratch", {"id": "b"}, {}) is False
    with pytest.raises(ValueError, match="is a key"):
        await run(upsert_typed, "typed_scratch", key, {"id": "z"})
    with pytest.raises(ValueError, match="not a column name"):
        await run(upsert_typed, 'typed_scratch"; drop', key, {})
    with pytest.raises(ValueError, match="not a column name"):
        await run(upsert_typed, "typed_scratch", {"id; drop": "a"}, {})
