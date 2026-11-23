"""Writes to the finance.ibkr_* tables: what a Flex statement says that the ledger has no home for.

Columns are IBKR's attribute names in snake_case (`ibOrderID` is `ib_order_id`). Each table's
types are the database's: `typed_values` reads them from the catalog and converts a row's
attributes (strings) to them, so a new column needs no change here. Four tables keep IBKR's
transactionID as `ibkr_transaction_id`, as their `transaction_id` is the ledger row's.

The personal fields of Account Information are stored in `ibkr_accounts`; nothing here logs a
value, and errors name the section and field only.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.modules.finance import typed_tables
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.typed_tables import Fields, TypedValueError, upsert_typed

NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
# The zone suffixes a Flex time can carry (Decision 10); any other fails closed.
_ZONES = {"EST": NEW_YORK, "EDT": NEW_YORK, "GMT": LONDON, "BST": LONDON}
# Standard time: in the hour that repeats when the clocks go back, the second one (fold 1).
_STANDARD = frozenset({"EST", "GMT"})

_MOMENT = re.compile(
    r"(\d{8}|\d{4}-\d{2}-\d{2})(?:[;, T]\s*(\d{6}|\d{2}:\d{2}:\d{2})(?:\s+([A-Za-z]+))?)?"
)

# Tables whose `transaction_id` is the ledger row's, IBKR's transactionID being a text column.
_LEDGER_ROW_TABLES = frozenset(
    {"ibkr_trades", "ibkr_cash_transactions", "ibkr_corporate_actions", "ibkr_transfers"}
)


def column_name(table: str, attribute: str) -> str:
    """The column an IBKR attribute is stored in."""
    name = typed_tables.column_name(attribute)
    if name == "transaction_id" and table in _LEDGER_ROW_TABLES:
        return "ibkr_transaction_id"
    return name


def parse_date(value: str, label: str) -> date:
    """A Flex date (`yyyyMMdd`, or ISO); a date-time keeps its date."""
    try:
        return typed_tables.parse_date(value, label, separators=";")
    except TypedValueError as error:
        raise FlexDataError(str(error)) from None


def parse_moment(value: str, label: str) -> datetime:
    """A Flex date or date-time as an aware datetime in the zone it names.

    A date alone is 00:00 in New York; a time needs a zone suffix of EST, EDT, GMT or BST.
    """
    found = _MOMENT.fullmatch(value.strip())
    if found is None:
        raise FlexDataError(f"{label} is not a date or date-time")
    day = parse_date(found[1], label)
    if found[2] is None:
        return datetime.combine(day, time(), NEW_YORK)
    suffix = (found[3] or "").upper()
    zone = _ZONES.get(suffix)
    if zone is None:
        raise FlexDataError(f"{label} has a time without a known time zone")
    clock = found[2].replace(":", "")
    hms = time(int(clock[:2]), int(clock[2:4]), int(clock[4:]), fold=int(suffix in _STANDARD))
    return datetime.combine(day, hms, zone)


def parse_decimal(value: str, label: str) -> Decimal:
    try:
        return typed_tables.parse_decimal(value, label)
    except TypedValueError as error:
        raise FlexDataError(str(error)) from None


async def typed_values(
    session: AsyncSession, table: str, section: str, attributes: Mapping[str, str]
) -> dict[str, object]:
    """The row's attributes that have a column in the table, as the column's type.

    An empty value is NULL. Attributes without a column (the keys, what the ledger holds)
    and columns of type uuid (the caller's references) are left out.
    """
    types = await typed_tables.column_types(session, table)
    try:
        # Two attributes of a row giving one column would overwrite each other.
        typed_tables.column_map(attributes)
    except ValueError as error:
        raise FlexDataError(f"{section}: {error}") from None
    values: dict[str, object] = {}
    for attribute, raw in attributes.items():
        column = column_name(table, attribute)
        kind = types.get(column)
        if kind is None or kind == "uuid":
            continue
        label = f"{section}.{attribute}"
        value = raw.strip()
        if not value:
            values[column] = None
        elif kind == "numeric":
            values[column] = parse_decimal(value, label)
        elif kind == "date":
            values[column] = parse_date(value, label)
        elif kind.startswith("timestamp"):
            values[column] = parse_moment(value, label)
        else:
            values[column] = raw
    return values


def _content_key(attributes: Mapping[str, str]) -> str:
    canonical = json.dumps(dict(attributes), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def trade_detail_key(attributes: Mapping[str, str]) -> str:
    """The key of a trade row other than an execution: its transactionID, else its tradeID,
    else a hash of its attributes."""
    for name in ("transactionID", "tradeID"):
        if attributes.get(name):
            return attributes[name]
    return _content_key(attributes)


def lot_key(attributes: Mapping[str, str]) -> str:
    """The key of an open position's lot: its originatingTransactionID, else a hash of its
    attributes."""
    return attributes.get("originatingTransactionID", "").strip() or _content_key(attributes)


def _fields_key(attributes: Mapping[str, str], names: tuple[str, ...]) -> str:
    # Named fields only: a query that adds fields later must not make the same row a new one.
    return _content_key({name: attributes.get(name, "").strip() for name in names})


def statement_of_funds_key(attributes: Mapping[str, str]) -> str:
    """The key of a Statement of Funds line within its statement: what the line is (its
    level, currency, dates, activity and transaction), not its amounts."""
    return _fields_key(
        attributes,
        (
            "levelOfDetail",
            "currency",
            "date",
            "reportDate",
            "activityCode",
            "activityDescription",
            "transactionID",
            "tradeID",
        ),
    )


def mtm_performance_key(attributes: Mapping[str, str]) -> str:
    """The key of an MTM Performance Summary row within its statement: the underlying it is
    for (or the total it is), not its amounts."""
    return _fields_key(
        attributes, ("assetCategory", "conid", "symbol", "description", "reportDate")
    )


async def _upsert(
    ctx: CommandContext,
    table: str,
    key_columns: tuple[str, ...],
    key: Fields,
    values: Fields,
) -> bool:
    """Insert the row, or update it when it differs; True when it wrote."""
    if set(key) != set(key_columns):
        raise ValueError(f"{table} is keyed by {', '.join(key_columns)}")
    return await upsert_typed(ctx, table, key, values)


async def upsert_accounts(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_accounts", ("account_id",), key, values)


async def upsert_instruments(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_instruments", ("instrument_id",), key, values)


async def upsert_trades(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_trades", ("transaction_id",), key, values)


async def upsert_cash_transactions(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_cash_transactions", ("transaction_id",), key, values)


async def upsert_corporate_actions(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_corporate_actions", ("transaction_id",), key, values)


async def upsert_transfers(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_transfers", ("transaction_id",), key, values)


async def upsert_positions(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_positions", ("snapshot_id",), key, values)


async def upsert_position_lots(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(
        ctx, "ibkr_position_lots", ("snapshot_id", "originating_transaction_id"), key, values
    )


async def upsert_trade_details(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(
        ctx, "ibkr_trade_details", ("account_id", "level_of_detail", "row_key"), key, values
    )


async def upsert_cash_reports(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(
        ctx,
        "ibkr_cash_reports",
        ("account_id", "currency_instrument_id", "from_date", "to_date"),
        key,
        values,
    )


async def upsert_nav(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_nav", ("account_id", "report_date"), key, values)


async def upsert_change_in_nav(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(
        ctx, "ibkr_change_in_nav", ("account_id", "from_date", "to_date"), key, values
    )


_PERIOD_ROW = ("account_id", "from_date", "to_date", "row_key")


async def upsert_statement_of_funds(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_statement_of_funds", _PERIOD_ROW, key, values)


async def upsert_mtm_performance(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(ctx, "ibkr_mtm_performance", _PERIOD_ROW, key, values)


async def upsert_prior_period_positions(ctx: CommandContext, key: Fields, values: Fields) -> bool:
    return await _upsert(
        ctx, "ibkr_prior_period_positions", ("account_id", "instrument_id", "date"), key, values
    )
