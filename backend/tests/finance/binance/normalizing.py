"""Running the Binance normaliser over rows a test builds: they are stored as raw records the way
a walk stores them, then normalised as a sync does, and the ledger read back."""

import json
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.binance.balances import Read, store_reports
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import VERSION, record_type, to_json
from lararium.modules.finance.binance.normalize import NormalizeReport, normalize, regenerate
from lararium.modules.finance.binance.parse import ApiValue
from lararium.modules.finance.ledger import find_unbalanced_transactions

from .fixtures import NOW_MS, build_response, build_row
from .syncing import CONNECTOR, context

type Rows = Sequence[Mapping[str, ApiValue]]


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP in this test")


def contents(label: str, rows: Rows) -> list[JSONValue]:
    """Raw record contents of one response set, as a walk stores them (Decision 5)."""
    stored = [to_json(dict(row)) for row in rows]
    if ENDPOINTS[label].id_fields:
        return stored
    seen: Counter[str] = Counter()
    out: list[JSONValue] = []
    for row in stored:
        canonical = json.dumps(row, sort_keys=True, separators=(",", ":"))
        seen[canonical] += 1
        out.append({"row": row, "occurrence": seen[canonical]})
    return out


async def ingest(
    session,
    connection_id: UUID,
    label: str,
    rows: Rows,
    *,
    sync_id: UUID | None = None,
    request: dict[str, JSONValue] | None = None,
) -> list[UUID]:
    """Store the rows as one response set of the label, answering `request` (its parameters:
    a variant's value, an isolated symbol)."""
    sync = sync_id or uuid.uuid4()
    async with command_context(session, CONNECTOR, "binance.fetch", {}) as ctx:
        return [
            await store_raw_record(
                ctx,
                connection_id=connection_id,
                record_type=record_type(label),
                request=request or {},
                content=content,
                connector_version=VERSION,
                sync_id=sync,
            )
            for content in contents(label, rows)
        ]


async def run_normalize(session, connection_id: UUID, *, again: bool = False) -> NormalizeReport:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await context(session, connection_id, http)
        return await (regenerate(ctx) if again else normalize(ctx))


async def normalize_rows(
    session, connection_id: UUID, *batches: tuple[str, Rows]
) -> NormalizeReport:
    """Store each (label, rows) batch, then normalise once."""
    for label, rows in batches:
        await ingest(session, connection_id, label, rows)
    return await run_normalize(session, connection_id)


async def list_pairs(run, connection, *pairs: tuple[str, str, str], at_ms: int = NOW_MS) -> None:
    """An `exchangeInfo` report listing these (symbol, base, quote)."""
    symbols = [
        build_row("exchange-info", symbol=symbol, baseAsset=base, quoteAsset=quote)
        for symbol, base, quote in pairs
    ]
    body = to_json(build_response("exchange-info", symbols))
    moment = at(at_ms)
    read = Read("exchange-info", body, moment, moment)
    await run(store_reports, connection, {"exchange-info": read})


async def postings(session, key: str) -> list[tuple[Any, ...]]:
    """(account, symbol, quantity, cost, cost symbol, price, price symbol, is_fee, memo) of the
    live transaction `key`, in order."""
    rows = await session.execute(
        text(
            "select a.name, i.symbol, p.quantity, p.cost_per_unit, ci.symbol as cost_symbol,"
            " p.price_per_unit, pi.symbol as price_symbol, p.is_fee, p.memo"
            " from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " left join finance.instruments ci on ci.id = p.cost_instrument_id"
            " left join finance.instruments pi on pi.id = p.price_instrument_id"
            " where t.external_id = :e and t.status <> 'void' order by p.position"
        ),
        {"e": key},
    )
    return [tuple(r) for r in rows]


def plain(rows: list[tuple[Any, ...]]) -> list[tuple[str, str, Decimal]]:
    """(account, symbol, quantity) only."""
    return [(r[0], r[1], r[2]) for r in rows]


async def transaction(session, key: str) -> Any:
    return (
        await session.execute(
            text(
                "select id, status, occurred_at, booking_date, occurred_tz, description"
                " from finance.transactions where external_id = :e"
            ),
            {"e": key},
        )
    ).one_or_none()


async def keys(session, connection_id: UUID) -> list[str]:
    """The external ids of the connection's live transactions."""
    found = await session.scalars(
        text(
            "select external_id from finance.transactions"
            " where connection_id = :c and status <> 'void' order by external_id"
        ),
        {"c": connection_id},
    )
    return list(found)


async def ledger(session, connection_id: UUID) -> list[tuple[Any, ...]]:
    """Every live posting of the connection: (key, account, symbol, quantity, is_fee, at)."""
    rows = await session.execute(
        text(
            "select t.external_id, a.name, i.symbol, p.quantity, p.is_fee, t.occurred_at"
            " from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where t.connection_id = :c and t.status <> 'void'"
            " order by t.external_id, p.position"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


async def holdings(session, connection_id: UUID) -> dict[tuple[str, str], Decimal]:
    """The sum of live postings per (account, symbol), zeros left out."""
    rows = await session.execute(
        text(
            "select a.name, i.symbol, sum(p.quantity) as q from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where t.connection_id = :c and t.status <> 'void'"
            " group by a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return {(r.name, r.symbol): r.q for r in rows if r.q != 0}


async def typed(session, table: str, connection_id: UUID) -> list[Any]:
    return list(
        (
            await session.execute(
                text(f"select * from finance.{table} where connection_id = :c"),
                {"c": connection_id},
            )
        ).all()
    )


async def assert_balanced_and_quiet(session, connection_id: UUID) -> None:
    """Every transaction balances, and another run writes nothing."""
    assert await find_unbalanced_transactions(session) == []
    before = await ledger(session, connection_id)
    report = await run_normalize(session, connection_id)
    assert (report.records, report.created, report.updated, report.removed) == (0, 0, 0, 0)
    assert report.new_flags == []
    assert await ledger(session, connection_id) == before
    changes = await session.scalar(
        text(
            "select changes from core.audit_log where command = 'binance.normalize'"
            " order by id desc limit 1"
        )
    )
    assert changes == {}


def at(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, UTC).replace(microsecond=(ms % 1000) * 1000)
