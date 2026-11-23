"""Finding or creating the instrument of an eToro row, and merging two listings that turn out
to be one (design §5.6).

An instrument is a listing: a security in one currency. One is found by eToro's instrument id
(`etoro_id`), then by ISIN in the same currency (a listing IBKR or Trading 212 holds), and
created when neither finds it; a hit lacking the id or the ISIN gets it. eToro's API gives no
ISIN, so an eToro instrument stands alone until a statement brings one (Task 11), which may
show it is a listing another source holds: `merge_instruments` makes them one.

An instrument's kind is eToro's instrument type (5 stocks → `equity`, 6 ETF → `etf`), from
the connection's market data (finance.etoro_instruments). An instrument whose type eToro has
not given yet is made with kind `other` and flagged while it stays so (`type_flags`); the type
reclassifies it when it arrives (`reclassify`). One of a type the ledger does not hold posts
nothing, and an instrument found to be one has what it posted voided.

eToro quotes some London listings in pence (GBX); the ledger lists them in pounds, prices
divided by 100 (`listing_currency`, as Trading 212's).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance.connector_run import listing_currency
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    InstrumentKind,
    add_identifiers,
    create_instrument,
    find_by_identifier,
    find_same_listing,
    get_currency,
    get_instrument,
    set_kind,
)

# eToro's instrument types the ledger holds (design §5.6); any other is not posted.
KINDS: Mapping[str, InstrumentKind] = {"5": "equity", "6": "etf"}
# eToro's precision of fractional units (design §5.6).
DECIMALS = 6
SCHEME = "etoro_id"


async def instrument_type(session: AsyncSession, instrument_id: int) -> str | None:
    """eToro's type of the instrument, as its market data gives it; None when none has."""
    found = await session.scalar(
        text(
            "select instrument_type_id::text from finance.etoro_instruments"
            " where instrument_id = :i and instrument_type_id is not null"
            " order by connection_id limit 1"
        ),
        {"i": instrument_id},
    )
    return found if isinstance(found, str) else None


def instrument_kind(type_id: str | None) -> InstrumentKind | None:
    """The kind an eToro type is; None for one the ledger does not hold. Without a type the
    kind is unknown: `other`."""
    if type_id is None:
        return "other"
    return KINDS.get(type_id)


async def listing(
    ctx: CommandContext,
    instrument_id: int,
    symbol: str,
    name: str,
    currency: str,
    isin: str | None,
) -> UUID | None:
    """The instrument eToro's id names, created if new; None for a type the ledger does not
    hold. `currency` is the asset currency (GBX: the listing is in GBP), needed to find by
    ISIN or to create; `isin` when a statement brings one."""
    kind = instrument_kind(await instrument_type(ctx.session, instrument_id))
    if kind is None:
        return None
    identifiers = [InstrumentIdentifier(SCHEME, str(instrument_id))]
    isin = (isin or "").strip() or None
    if isin is not None:
        identifiers.append(InstrumentIdentifier("isin", isin))

    found = await find_by_identifier(ctx.session, SCHEME, str(instrument_id))
    if found is None:
        if not currency:
            raise FinanceValidationError("an eToro instrument new to the ledger needs its currency")
        quote = await get_currency(ctx.session, listing_currency(currency)[0])
        if isin is not None:
            found = await find_same_listing(ctx.session, isin, quote.id)
        if found is None:
            # The registry holds an identifier once: a second listing of one ISIN goes without.
            free = [
                i
                for i in identifiers
                if await find_by_identifier(ctx.session, i.scheme, i.value) is None
            ]
            created = await create_instrument(
                ctx,
                kind=kind,
                symbol=symbol,
                name=name.strip() or symbol,
                decimals=DECIMALS,
                quote_instrument_id=quote.id,
                identifiers=free,
            )
            return created.id
    await add_identifiers(ctx, found.id, identifiers)
    if found.kind == "other" and kind != "other":
        await set_kind(ctx, found.id, kind)
    return found.id


async def reclassify(ctx: CommandContext, connection_id: UUID) -> list[UUID]:
    """Give each instrument made without a type the kind the connection's market data now
    says it is; returns those it says the ledger does not hold (what they posted is voided:
    records.void_unsupported)."""
    rows = await ctx.session.execute(
        text(
            "select i.id, m.instrument_type_id::text as type from finance.instruments i"
            " join finance.instrument_identifiers ii"
            " on ii.instrument_id = i.id and ii.scheme = 'etoro_id'"
            " join finance.etoro_instruments m"
            " on m.connection_id = :c and m.instrument_id::text = ii.value"
            " where i.kind = 'other' and m.instrument_type_id is not null"
        ),
        {"c": connection_id},
    )
    unsupported: list[UUID] = []
    for row in rows.all():
        kind = KINDS.get(row.type)
        if kind is None:
            unsupported.append(row.id)
        else:
            await set_kind(ctx, row.id, kind)
    return sorted(unsupported)


UNKNOWN = "instrument type unknown"
UNSUPPORTED = "instrument type unsupported"


async def type_flags(
    session: AsyncSession, connection_id: UUID, account_ids: Sequence[UUID]
) -> set[str]:
    """The accounts' instruments (posted or reported on them) made without a type: those the
    market data has not typed yet, and those it typed as one the ledger does not hold."""
    rows = await session.execute(
        text(
            "select i.symbol, m.instrument_type_id::text as type from finance.instruments i"
            " join finance.instrument_identifiers ii"
            " on ii.instrument_id = i.id and ii.scheme = 'etoro_id'"
            " left join finance.etoro_instruments m"
            " on m.connection_id = :c and m.instrument_id::text = ii.value"
            " where i.kind = 'other' and ("
            " exists (select 1 from finance.postings p"
            " where p.instrument_id = i.id and p.account_id = any(cast(:a as uuid[])))"
            " or exists (select 1 from finance.balance_snapshots s"
            " where s.instrument_id = i.id and s.account_id = any(cast(:a as uuid[]))))"
        ),
        {"c": connection_id, "a": list(account_ids)},
    )
    flags: set[str] = set()
    for row in rows:
        if row.type is None:
            flags.add(f"{UNKNOWN} {row.symbol}")
        elif row.type not in KINDS:
            flags.add(f"{UNSUPPORTED} {row.symbol}")
    return flags


CURRENCY_UNKNOWN = "instrument currency unknown"


async def currency_flags(session: AsyncSession, connection_id: UUID) -> set[str]:
    """The instruments the connection's positions and trades name that the ledger does not
    hold yet because neither the aggregate nor the breakdown has given their currency (an
    order lookup's `asset.currency` is not taken: the probe found it to differ). Named by
    symbol: the market data's, else an order's or the breakdown's, else eToro's id."""
    rows = await session.execute(
        text(
            "with named as ("
            " select instrument_id from finance.etoro_trades"
            " where connection_id = :c and instrument_id is not null"
            " union select instrument_id from finance.etoro_pnl_positions"
            " where connection_id = :c),"
            " priced as ("
            " select instrument_id from finance.etoro_aggregate_instruments"
            " where connection_id = :c and asset_currency is not null"
            # A breakdown position's instrument is its own instrumentId, else its parent's.
            " union select coalesce(p.instrument_id, i.instrument_id)"
            " from finance.etoro_breakdown_positions p"
            " join finance.etoro_breakdown_instruments i on i.connection_id = p.connection_id"
            " and i.as_of_date = p.as_of_date and i.item_index = p.instrument_index"
            " where p.connection_id = :c and p.asset_currency is not null)"
            " select n.instrument_id::text as id,"
            " coalesce((select m.symbol_full from finance.etoro_instruments m"
            " where m.connection_id = :c and m.instrument_id = n.instrument_id),"
            " (select o.asset_symbol from finance.etoro_orders o where o.connection_id = :c"
            " and o.asset_instrument_id = n.instrument_id and o.asset_symbol is not null"
            " order by o.order_id limit 1),"
            " (select b.symbol from finance.etoro_breakdown_instruments b"
            " where b.connection_id = :c and b.instrument_id = n.instrument_id"
            " and b.symbol is not null order by b.as_of_date desc limit 1)) as symbol"
            " from named n"
            " where not exists (select 1 from priced where priced.instrument_id = n.instrument_id)"
            " and not exists (select 1 from finance.instrument_identifiers ii"
            " where ii.scheme = 'etoro_id' and ii.value = n.instrument_id::text)"
        ),
        {"c": connection_id},
    )
    return {f"{CURRENCY_UNKNOWN} {row.symbol or f'eToro {row.id}'}" for row in rows}


# Merging ---------------------------------------------------------------------------------

# Rows of these tables that would duplicate one of the kept instrument's are dropped (design
# §5.6): a price for the same date, currency and source.
_DISCARDED_DUPLICATES = frozenset({"prices"})


@dataclass(frozen=True)
class _Reference:
    table: str
    column: str


@dataclass(frozen=True)
class _Unique:
    table: str
    columns: tuple[str, ...]


async def _references(session: AsyncSession) -> list[_Reference]:
    """Every column with a foreign key to finance.instruments, from the catalog, so a table
    added later is covered too. Each is in the finance schema and a single column."""
    rows = await session.execute(
        text(
            "select n.nspname as schema, c.relname as tbl, a.attname as col,"
            " cardinality(k.conkey) as width"
            " from pg_constraint k"
            " join pg_class c on c.oid = k.conrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " join pg_attribute a on a.attrelid = c.oid and a.attnum = k.conkey[1]"
            " where k.contype = 'f' and k.confrelid = 'finance.instruments'::regclass"
            " order by c.relname, a.attname"
        )
    )
    found: list[_Reference] = []
    for row in rows:
        if row.schema != "finance" or row.width != 1:
            raise FinanceValidationError(
                f"{row.schema}.{row.tbl} refers to instruments in a way a merge cannot move"
            )
        found.append(_Reference(row.tbl, row.col))
    return found


async def _uniques(session: AsyncSession, table: str, columns: set[str]) -> list[_Unique]:
    """The table's unique constraints and indexes that hold any of `columns`. One holding an
    expression is refused: what it holds after the merge cannot be checked."""
    rows = await session.execute(
        text(
            "select x.indexrelid, x.indexprs is not null as expression,"
            " array_agg(a.attname order by k.ord) as cols"
            " from pg_index x"
            " join pg_class c on c.oid = x.indrelid"
            " join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'finance'"
            " cross join lateral unnest(x.indkey::int2[]) with ordinality as k(attnum, ord)"
            " left join pg_attribute a on a.attrelid = c.oid and a.attnum = k.attnum"
            " where x.indisunique and c.relname = :t"
            " group by x.indexrelid, x.indexprs"
        ),
        {"t": table},
    )
    found: list[_Unique] = []
    for row in rows:
        names = tuple(name for name in row.cols if name is not None)
        if not columns & set(names):
            continue
        if row.expression:
            raise FinanceValidationError(f"{table} has a unique index a merge cannot check")
        found.append(_Unique(table, names))
    return found


def _mapped(alias: str, column: str, moved: set[str]) -> str:
    """The column of a row as the merge leaves it: the dropped instrument becomes the kept."""
    if column not in moved:
        return f'{alias}."{column}"'
    return f'(case when {alias}."{column}" = :drop then :keep else {alias}."{column}" end)'


def _conflicts(unique: _Unique, moved: set[str]) -> str:
    """Rows holding the dropped instrument that would equal another row on `unique` once
    repointed (two rows equal on every column of a unique key)."""
    same = " and ".join(
        f"{_mapped('a', c, moved)} = {_mapped('b', c, moved)}" for c in unique.columns
    )
    holds = " or ".join(f'a."{c}" = :drop' for c in sorted(moved))
    table = f'finance."{unique.table}"'
    return f"{table} a join {table} b on a.ctid <> b.ctid and {same} where ({holds})"


async def merge_instruments(ctx: CommandContext, keep: UUID, drop: UUID) -> None:
    """Make `drop` one with `keep` (design §5.6): every row that references `drop` (postings
    and their cost and price instruments, prices, balance snapshots, identifiers, the typed
    tables, and any other column with a foreign key to finance.instruments) is repointed to
    `keep`, then `drop` is deleted. A price of `drop` on a date, currency and source `keep`
    has one for is discarded, `keep`'s staying. Any other row that would then duplicate one
    of `keep`'s on a unique key fails the merge naming its table, before anything is
    written; so do two listings in different currencies, and an identifier scheme both hold.
    """
    session = ctx.session
    if keep == drop:
        raise FinanceValidationError("an instrument can't be merged into itself")
    kept = await get_instrument(session, keep)
    dropped = await get_instrument(session, drop)
    if kept.kind == "fiat" or dropped.kind == "fiat":
        raise FinanceValidationError("a currency can't be merged")
    if kept.quote_instrument_id != dropped.quote_instrument_id:
        raise FinanceValidationError("two listings in different currencies can't be merged")
    if both := {i.scheme for i in kept.identifiers} & {i.scheme for i in dropped.identifiers}:
        raise FinanceValidationError(
            f"both instruments have an identifier of scheme {', '.join(sorted(both))}"
        )

    references = await _references(session)
    by_table: dict[str, set[str]] = {}
    for reference in references:
        by_table.setdefault(reference.table, set()).add(reference.column)
    params = {"keep": keep, "drop": drop}
    discarded: dict[str, str] = {}
    for table, moved in sorted(by_table.items()):
        for unique in await _uniques(session, table, moved):
            clash = _conflicts(unique, moved)
            if table in _DISCARDED_DUPLICATES:
                discarded.setdefault(table, "")
                discarded[table] += f" or ctid in (select a.ctid from {clash})"
                continue
            if await session.scalar(text(f"select exists (select 1 from {clash})"), params):
                raise FinanceValidationError(
                    f"{table} holds rows of both instruments that a merge would make one"
                )

    moved_rows: dict[str, JSONValue] = {}
    for table, condition in sorted(discarded.items()):
        result = await session.execute(
            text(f'delete from finance."{table}" where false{condition} returning 1'), params
        )
        moved_rows[f"{table} discarded"] = len(result.all())
    for reference in references:
        result = await session.execute(
            text(
                f'update finance."{reference.table}" set "{reference.column}" = :keep'
                f' where "{reference.column}" = :drop returning 1'
            ),
            params,
        )
        count = len(result.all())
        if count:
            moved_rows[f"{reference.table}.{reference.column}"] = count
    await session.execute(text("delete from finance.instruments where id = :drop"), params)
    if dropped.decimals > kept.decimals:
        # The finer of the two: the dropped one's quantities must still be held exactly.
        await session.execute(
            text("update finance.instruments set decimals = :d where id = :keep"),
            {**params, "d": dropped.decimals},
        )

    record = await get_instrument(session, keep)
    data: dict[str, JSONValue] = {
        "id": str(record.id),
        "symbol": record.symbol,
        "decimals": record.decimals,
        "identifiers": [{"scheme": i.scheme, "value": i.value} for i in record.identifiers],
    }
    ctx.emit("finance.instrument.merged", keep, {"kept": str(keep), "dropped": str(drop)})
    ctx.record_change(f"instrument:{drop}", {"merged_into": str(keep), "moved": moved_rows})
    ctx.record_change(f"instrument:{keep}", data)
