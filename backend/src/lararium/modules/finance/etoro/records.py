"""eToro items that move money or shares, as connector transactions (design §5.7).

**Opens and closes** of the Trading account, one transaction each, keyed `open:<positionId>`
and `close:<positionId>` (Decision 9). A position's open comes from trade history once it has
closed (what closed is what was opened: a partial close's remainder is a position of its own,
with `parentPositionId`, design §6), else from the latest PnL report that lists it, so it
survives the position leaving PnL. An open adds the units at `amount / units` in the account's
currency (`accountCurrency`) and takes `amount` out; a close takes the units out at
`(investment + netProfit) / units` and puts that cash in. A non-zero execution fee or tax
(orders:lookup), and a close's trade-history `fees`, are fee postings (`is_fee`) countered
on Unassigned. A position that is not real (`settlementTypeID` 1, `settlementType` real), has
leverage other than 1, or is copied (a `mirrorID` or `socialTradeId`) is flagged and posts
nothing; so does one on an instrument of a type the ledger does not hold. A run applies the
positions its records name (this sync's PnL, trade history and lookups; every one on a
replay), each from everything stored.

**eToro Money** (key `cash:<id>`): only a `settled` row posts, its `amount` in its currency, a
debit negative. A bank transfer goes through transfer_contra as a bank transfer, naming the
other bank's account when it is a UK one (a GB IBAN, or a sort code and account number); an
internal transfer names the Trading account when its counterparty's name says trading (Task 0,
check 4), and goes to Unassigned when it names MoneyFarm or the ISA (design §5.12) or neither
(flagged); a card payment or balance adjustment goes to Unassigned.

**Opening balances** (design §5.5): eToro Money's earliest balance-history snapshot, less
what its records post up to the end of that day, is one opening adjustment per currency,
against Opening balances, the day before its earliest record; every run sets it afresh, so
records arriving later move it, and records reaching back leave none. eToro Trading gets none.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from uuid import UUID

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance import models
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.etoro import accounts
from lararium.modules.finance.etoro.state import (
    LONDON,
    EtoroDataError,
    Item,
    Record,
    Report,
    Run,
    flat,
    id_at,
    moment_at,
    number_at,
    object_at,
    objects_at,
    record_key,
    records_of,
    text_at,
)
from lararium.modules.finance.identifiers import get_or_create_identifier, normalize_identifier
from lararium.modules.finance.instruments import InstrumentRecord, get_instrument
from lararium.modules.finance.ledger import CounterpartyFields, PostingInput, compute_residuals
from lararium.modules.finance.money import to_plain_str, tolerance

OPEN = "open:"
CLOSE = "close:"
OPENING = "opening:"
OPENING_DESCRIPTION = "Opening balance adjustment"
UNSUPPORTED_POSITION = "a CFD, leveraged or copied position"
UNKNOWN_INTERNAL = "an internal transfer names no account Lararium knows"
# A price per unit is cash over units, to this place.
_PER_UNIT = Decimal(1).scaleb(-10)
# eToro Money's enums (SPEC): a value not listed is flagged and posts nothing.
_STATUSES = frozenset(
    {"failed", "authorized", "settled", "rejected", "returned", "expired", "unknown"}
)
_TYPES = {
    "bankTransfer": "Bank transfer",
    "internalTransfer": "Internal transfer",
    "card": "Card payment",
    "balanceAdjustment": "Balance adjustment",
}
_SIGNS = {"credit": 1, "debit": -1}
# A UK IBAN: GB, its check digits, the bank's four letters, the sort code, the account number.
_UK_IBAN = re.compile(r"GB[0-9]{2}[A-Z]{4}([0-9]{6})([0-9]{8})")
_MONEYFARM = re.compile(r"moneyfarm|\bisa\b", re.IGNORECASE)
_TRADING = re.compile(r"trading", re.IGNORECASE)


# Positions -------------------------------------------------------------------------------


@dataclass
class Position:
    """What the records say about one position: the latest PnL and breakdown items that list
    it, its trade-history close, and its order's lookup."""

    position_id: str
    pnl: tuple[Record, Item] | None = None
    breakdown: tuple[Record, Item] | None = None
    trade: Record | None = None
    lookup: Record | None = None

    def execution(self) -> Item | None:
        if self.lookup is None:
            return None
        for item in objects_at(self.lookup.content, "positionExecutions"):
            if text_at(item, "positionId") == self.position_id:
                return item
        return None


def _pnl_items(records: Iterable[Record]) -> dict[str, tuple[Record, Item]]:
    """Each position the PnL records list (in order: the last one's wins), with its item."""
    found: dict[str, tuple[Record, Item]] = {}
    for record in records:
        for item in objects_at(record.content, "clientPortfolio.positions"):
            found[id_at(record, item, "positionID")] = (record, item)
    return found


def _breakdown_items(records: Iterable[Record]) -> dict[str, tuple[Record, Item]]:
    """Each position the breakdown records list (the last one's wins), with its item."""
    found: dict[str, tuple[Record, Item]] = {}
    for record in records:
        for instrument in objects_at(record.content, "instruments"):
            for item in objects_at(instrument, "positions"):
                found[id_at(record, item, "positionId")] = (record, item)
    return found


_STORED_ITEMS = {
    "pnl": ("etoro_pnl_positions", _pnl_items),
    "breakdown": ("etoro_breakdown_positions", _breakdown_items),
}


async def _stored_item(run: Run, endpoint: str, position_id: str) -> tuple[Record, Item] | None:
    """The position's item in the record of the latest day whose report of the endpoint
    listed it (its typed rows say which)."""
    table, items = _STORED_ITEMS[endpoint]
    raw_record_id = await run.session.scalar(
        text(
            f"select raw_record_id from finance.{table} where connection_id = :c"
            " and position_id = cast(:p as numeric) order by as_of_date desc limit 1"
        ),
        {"c": run.connection_id, "p": position_id},
    )
    if raw_record_id is None:
        return None
    return items(await records_of(run.session, [raw_record_id])).get(position_id)


async def positions(
    run: Run,
    reports: Sequence[Report],
    trades: Mapping[str, Record],
    lookups: Mapping[str, Record],
    given: set[str],
    replay: bool,
) -> list[Position]:
    """The positions the run applies (module doc). `trades` and `lookups` are every stored
    one (by position and by order id), `given` the position ids this run's trade history and
    lookups name."""
    ordered = [r.record for r in sorted(reports, key=lambda r: r.order)]
    pnl = _pnl_items(r for r in ordered if r.endpoint == "pnl")
    breakdown = _breakdown_items(r for r in ordered if r.endpoint == "breakdown")
    wanted = set(pnl) | given
    if replay:
        wanted |= set(trades)
        for lookup in lookups.values():
            wanted |= {
                id_at(lookup, item, "positionId")
                for item in objects_at(lookup.content, "positionExecutions")
            }
    by_execution: dict[str, Record] = {}
    for lookup in lookups.values():
        for item in objects_at(lookup.content, "positionExecutions"):
            by_execution.setdefault(id_at(lookup, item, "positionId"), lookup)
    found: list[Position] = []
    for position_id in sorted(wanted, key=int):
        position = Position(position_id, pnl.get(position_id), breakdown.get(position_id))
        position.trade = trades.get(position_id)
        if not replay:
            if position.pnl is None:
                position.pnl = await _stored_item(run, "pnl", position_id)
            if position.breakdown is None:
                position.breakdown = await _stored_item(run, "breakdown", position_id)
        order_id = ""
        if position.trade is not None:
            order_id = text_at(position.trade.content, "orderId")
        elif position.pnl is not None:
            order_id = text_at(position.pnl[1], "orderID")
        position.lookup = lookups.get(order_id) or by_execution.get(position_id)
        found.append(position)
    return found


def _supported(position: Position) -> bool:
    """Real, unleveraged and not copied, as every source that says so agrees; one with no
    source stating its settlement or its leverage is not taken to be."""
    pnl = position.pnl[1] if position.pnl is not None else None
    breakdown = position.breakdown[1] if position.breakdown is not None else None
    trade = position.trade.content if position.trade is not None else None
    lookup = position.lookup.content if position.lookup is not None else None
    leverage = [text_at(i, "leverage") for i in (pnl, breakdown, trade)]
    leverage.append(text_at(lookup, "asset.leverage"))
    leverage = [v for v in leverage if v]
    settled = [text_at(pnl, "settlementTypeID") or None]
    settled.append(text_at(breakdown, "settlementType").lower() or None)
    settled.append(text_at(lookup, "asset.settlementType").lower() or None)
    real = [v in ("1", "real") for v in settled if v is not None]
    copied = [text_at(pnl, "mirrorID"), text_at(trade, "socialTradeId")]
    return (
        bool(leverage)
        and all(Decimal(v) == 1 for v in leverage)
        and bool(real)
        and all(real)
        and all(Decimal(v) == 0 for v in copied if v)
    )


def _per_unit(total: Decimal, quantity: Decimal) -> Decimal:
    """total / quantity to _PER_UNIT's place, without trailing zeros."""
    with localcontext(prec=80):
        value = (total / quantity).quantize(_PER_UNIT, rounding=ROUND_HALF_EVEN)
    trimmed = value.normalize()
    exponent = trimmed.as_tuple().exponent
    assert isinstance(exponent, int)  # finite
    return trimmed if exponent <= 0 else trimmed.quantize(Decimal(1))


def _plain(quantity: Decimal) -> str:
    return to_plain_str(quantity.copy_abs().normalize())


def _fees(
    account_id: UUID, unassigned: UUID, currency_id: UUID, fees: Sequence[tuple[Decimal, str]]
) -> list[PostingInput]:
    """Each fee on the account, then Unassigned's counterpart."""
    postings = [
        PostingInput(account_id, currency_id, -amount.copy_abs(), is_fee=True, memo=memo)
        for amount, memo in fees
        if amount != 0
    ]
    total = sum((p.quantity for p in postings), Decimal(0))
    if total != 0:
        postings.append(PostingInput(unassigned, currency_id, -total))
    return postings


async def _rounding(
    run: Run,
    position_id: str,
    postings: list[PostingInput],
    unassigned: UUID,
    currency: InstrumentRecord,
    quantity: Decimal,
) -> None:
    """A residual within the ledger's tolerance stays; one within what the price's rounding
    explains (|quantity| times half its last place) is posted to Unassigned (memo
    "rounding", design §5.7); more fails the run, naming the position."""
    allowed = tolerance(currency.decimals) + quantity.copy_abs() * _PER_UNIT / 2
    for instrument_id, residual in compute_residuals(postings).items():
        decimals = (await get_instrument(run.session, instrument_id)).decimals
        if residual.copy_abs() <= tolerance(decimals):
            continue
        if instrument_id != currency.id or residual.copy_abs() > allowed:
            raise EtoroDataError(
                f"eToro position {position_id}: its cash differs from its units times price"
                " by more than rounding"
            )
        postings.append(PostingInput(unassigned, instrument_id, -residual, memo="rounding"))


async def _account_currency(run: Run) -> InstrumentRecord:
    if not run.account_currency:
        raise EtoroDataError("eToro has not given the Trading account's currency")
    return await run.currency(run.account_currency)


async def _post_open(run: Run, position: Position, instrument_id: UUID) -> UUID | None:
    if position.trade is not None:
        record, item = position.trade, position.trade.content
        occurred_at = moment_at(record, item, "openTimestamp")
        units, amount = number_at(record, item, "units"), number_at(record, item, "investment")
    elif position.pnl is not None:
        record, item = position.pnl
        occurred_at = moment_at(record, item, "openDateTime")
        units, amount = number_at(record, item, "units"), number_at(record, item, "amount")
    else:
        return None
    units, amount = units.copy_abs(), amount.copy_abs()
    if units == 0:
        raise EtoroDataError(f"eToro position {position.position_id} has no units")
    currency = await _account_currency(run)
    unassigned = await run.system_account("unassigned")
    postings = [
        PostingInput(
            run.trading,
            instrument_id,
            units,
            cost_per_unit=_per_unit(amount, units),
            cost_instrument_id=currency.id,
        )
    ]
    if amount != 0:
        postings.append(PostingInput(run.trading, currency.id, -amount))
    execution = position.execution()
    if execution is not None and position.lookup is not None:
        opening = object_at(execution, "openingData")
        fees = [
            (number_at(position.lookup, opening, "fees"), "Execution fee"),
            (number_at(position.lookup, opening, "taxes"), "Execution tax"),
        ]
        postings += _fees(run.trading, unassigned, currency.id, fees)
    await _rounding(run, position.position_id, postings, unassigned, currency, units)
    description = f"Buy {_plain(units)} {await run.symbol(instrument_id)}"
    return await run.post(f"{OPEN}{position.position_id}", occurred_at, description, postings)


async def _post_close(run: Run, position: Position, instrument_id: UUID) -> UUID:
    assert position.trade is not None
    record, item = position.trade, position.trade.content
    units = number_at(record, item, "units").copy_abs()
    if units == 0:
        raise EtoroDataError(f"eToro position {position.position_id} has no units")
    proceeds = number_at(record, item, "investment") + number_at(record, item, "netProfit")
    if proceeds < 0:
        raise EtoroDataError(f"eToro position {position.position_id} closed for less than 0")
    currency = await _account_currency(run)
    unassigned = await run.system_account("unassigned")
    postings = [
        PostingInput(
            run.trading,
            instrument_id,
            -units,
            price_per_unit=_per_unit(proceeds, units),
            price_instrument_id=currency.id,
        )
    ]
    if proceeds != 0:
        postings.append(PostingInput(run.trading, currency.id, proceeds))
    postings += _fees(
        run.trading, unassigned, currency.id, [(number_at(record, item, "fees"), "Fee")]
    )
    await _rounding(run, position.position_id, postings, unassigned, currency, units)
    description = f"Sell {_plain(units)} {await run.symbol(instrument_id)}"
    return await run.post(
        f"{CLOSE}{position.position_id}",
        moment_at(record, item, "closeTimestamp"),
        description,
        postings,
    )


async def link_open(run: Run, position_id: str, transaction_id: UUID | None) -> None:
    """Link the position's execution (its first, should two lookups list it) to its open."""
    await run.session.execute(
        text(
            "with first as (select parent_order_id, item_index"
            " from finance.etoro_position_executions where connection_id = :c"
            " and position_id = cast(:p as numeric) order by parent_order_id, item_index limit 1)"
            " update finance.etoro_position_executions e set transaction_id ="
            " case when (e.parent_order_id, e.item_index) = (select * from first)"
            " then cast(:t as uuid) end"
            " where e.connection_id = :c and e.position_id = cast(:p as numeric)"
            " and e.transaction_id is distinct from case when (e.parent_order_id, e.item_index)"
            " = (select * from first) then cast(:t as uuid) end"
        ),
        {"c": run.connection_id, "p": position_id, "t": transaction_id},
    )


async def position(run: Run, position: Position) -> None:
    """Post (or void) the position's open and close, and type its close."""
    key = position.position_id
    if position.pnl is None and position.trade is None:
        return  # a lookup alone says nothing to post
    instrument_id: UUID | None = None
    if not _supported(position):
        run.flag(UNSUPPORTED_POSITION)
    else:
        source = position.trade.content if position.trade is not None else None
        etoro_id = text_at(source, "instrumentId") if source is not None else ""
        if not etoro_id and position.pnl is not None:
            etoro_id = text_at(position.pnl[1], "instrumentID")
        instrument_id = await run.instrument(etoro_id)
    opened = closed = None
    if instrument_id is not None:
        opened = await _post_open(run, position, instrument_id)
        if position.trade is not None:
            closed = await _post_close(run, position, instrument_id)
    if opened is None:
        await run.void(f"{OPEN}{key}")
    if closed is None:
        await run.void(f"{CLOSE}{key}")
    await link_open(run, key, opened)
    if position.trade is not None:
        await trade_row(run, position.trade, closed, instrument_id)


async def trade_row(
    run: Run, record: Record, transaction_id: UUID | None, instrument_id: UUID | None
) -> None:
    values = await run.typed("trade_history", "etoro_trades", flat(record.content))
    await run.upsert(
        "etoro_trades",
        {"connection_id": run.connection_id, "external_id": record_key(record)},
        values
        | {
            "transaction_id": transaction_id,
            "finance_instrument_id": instrument_id,
            "raw_record_id": record.id,
        },
    )


# eToro Money -----------------------------------------------------------------------------


def _bank_account(identifiers: Sequence[Item]) -> str | None:
    """The UK bank account the counterparty's identifiers name, in the registry's form; None
    for none, or a bank account outside the UK."""
    by_name = {text_at(i, "name").casefold(): text_at(i, "value") for i in identifiers}
    iban = re.sub(r"\s", "", by_name.get("iban", "")).upper()
    if (found := _UK_IBAN.fullmatch(iban)) is not None:
        value = f"{found[1]}:{found[2]}"
    else:
        sort_code = by_name.get("sortcode", "")
        number = by_name.get("bankaccountnumber", "") or by_name.get("accountnumber", "")
        if not sort_code or not number:
            return None
        value = f"{sort_code}:{number}"
    try:
        return normalize_identifier("uk_bank_account", value)
    except FinanceValidationError:
        return None


async def _counterparty(run: Run, record: Record, kind: str) -> tuple[CounterpartyFields, bool]:
    """What the row says about the other side; and whether it goes through transfer_contra
    (a movement with another account) rather than to Unassigned."""
    content = record.content
    name = text_at(content, "counterparty.name") or None
    if kind == "bankTransfer":
        value = _bank_account(objects_at(content, "bankTransferTransactionDetails.bankIdentifier"))
        identifier = None
        if value is not None:
            identifier = await run.shared(
                ("identifier", "uk_bank_account", value),
                lambda cmd: get_or_create_identifier(cmd, "uk_bank_account", value),
            )
        reference = text_at(content, "bankTransferTransactionDetails.paymentReference") or None
        return CounterpartyFields(identifier, name, reference, bank_transfer=True), True
    if kind == "internalTransfer":
        trading = _TRADING.search(name or "") is not None
        moneyfarm = _MONEYFARM.search(name or "") is not None
        identifier = None
        if trading and not moneyfarm:
            identifier = await accounts.trading_identifier(run)
        elif not moneyfarm:
            run.flag(UNKNOWN_INTERNAL)
        return CounterpartyFields(identifier, name), True
    return CounterpartyFields(name=name), False


async def cash(run: Run, record: Record) -> None:
    content = record.content
    key = record_key(record)
    etoro_id = normalize_identifier("etoro_account", text_at(content, "accountId"))
    account_id = run.accounts.get(etoro_id)
    if account_id is None:
        raise EtoroDataError("an eToro Money transaction is of an account the balances omit")
    status, kind = text_at(content, "status"), text_at(content, "transactionType")
    direction = text_at(content, "direction")
    transaction_id = None
    if status not in _STATUSES:
        run.flag_value("cash_transactions", "status", status)
    elif kind not in _TYPES:
        run.flag_value("cash_transactions", "transactionType", kind)
    elif direction not in _SIGNS:
        run.flag_value("cash_transactions", "direction", direction)
    elif status == "settled":
        amount = number_at(record, content, "amount").copy_abs() * _SIGNS[direction]
        if amount != 0:
            currency = await run.currency(text_at(content, "currency"))
            counterparty, through = await _counterparty(run, record, kind)
            contra = (
                await run.contra(key, account_id, 0, counterparty.identifier_id)
                if through
                else await run.system_account("unassigned")
            )
            description = _TYPES[kind]
            if kind == "card":
                description = text_at(content, "cardTransactionDetails.merchantName") or description
            transaction_id = await run.post(
                key,
                moment_at(record, content, "postedAt"),
                description,
                [
                    PostingInput(account_id, currency.id, amount),
                    PostingInput(contra, currency.id, -amount),
                ],
                counterparty,
            )
    if transaction_id is None:
        await run.void(key)
    endpoint = "cash_transactions"
    values = await run.typed(endpoint, "etoro_cash_transactions", flat(content))
    item_id = values.pop("id")
    await run.upsert(
        "etoro_cash_transactions",
        {"connection_id": run.connection_id, "id": item_id},
        values
        | {
            "transaction_id": transaction_id,
            "finance_account_id": account_id,
            "raw_record_id": record.id,
        },
    )
    parent = {"connection_id": run.connection_id, "cash_transaction_id": item_id}
    identifiers = objects_at(content, "bankTransferTransactionDetails.bankIdentifier")
    for n, identifier in enumerate(identifiers):
        values = await run.typed(
            endpoint,
            "etoro_cash_bank_identifiers",
            flat(identifier),
            "bankTransferTransactionDetails.bankIdentifier[].",
        )
        await run.upsert(
            "etoro_cash_bank_identifiers",
            parent | {"item_index": n},
            values | {"raw_record_id": record.id},
        )
    await run.prune("etoro_cash_bank_identifiers", parent, len(identifiers))


# Openings --------------------------------------------------------------------------------


def _not_opening() -> ColumnElement[bool]:
    return func.coalesce(models.Transaction.external_id, "").not_like(f"{OPENING}%")


async def holdings(
    session: AsyncSession, account_id: UUID, at: datetime, *also: ColumnElement[bool]
) -> dict[UUID, Decimal]:
    """The account's quantity of each instrument: its non-void postings that occurred at or
    before `at` (and meet `also`)."""
    t, p = models.Transaction, models.Posting
    return {
        row.instrument_id: Decimal(row.total)
        for row in await session.execute(
            select(p.instrument_id, func.sum(p.quantity).label("total"))
            .join(t, t.id == p.transaction_id)
            .where(p.account_id == account_id, t.status != "void", t.occurred_at <= at, *also)
            .group_by(p.instrument_id)
        )
    }


def end_of(day: date) -> datetime:
    """A balance-history snapshot's moment: the end of its date, in UTC (design §5.8)."""
    return datetime.combine(day, time.max, UTC)


async def openings(run: Run) -> None:
    """Set each eToro Money account's opening adjustment (module doc)."""
    t, p = models.Transaction, models.Posting
    live = t.status != "void"
    for etoro_id, account_id in sorted(run.accounts.items()):
        if run.account_types.get(etoro_id) != "Cash":
            continue
        earliest = (
            await run.session.execute(
                text(
                    "select snapshot_date, cash, currency"
                    " from finance.etoro_balance_account_snapshots"
                    " where connection_id = :c and lower(account_id) = :a and cash is not null"
                    " order by snapshot_date, item_index limit 1"
                ),
                {"c": run.connection_id, "a": etoro_id},
            )
        ).one_or_none()
        prefix = f"{OPENING}{etoro_id}:"
        current = {
            UUID(key.removeprefix(prefix))
            for key in await run.session.scalars(
                select(t.external_id).where(
                    t.connection_id == run.connection_id, t.external_id.like(f"{prefix}%"), live
                )
            )
            if key is not None
        }
        found: dict[UUID, Decimal] = {}
        day: date | None = None
        held: dict[UUID, Decimal] = {}
        if earliest is not None:
            day = earliest.snapshot_date
            found[(await run.currency(earliest.currency.upper())).id] = Decimal(earliest.cash)
            held = await holdings(
                run.session, account_id, end_of(earliest.snapshot_date), _not_opening()
            )
        first = await run.session.scalar(
            select(func.min(t.booking_date))
            .join(p, p.transaction_id == t.id)
            .where(p.account_id == account_id, live, _not_opening())
        )
        opening_balances = await run.system_account("opening_balances")
        for instrument_id in sorted(set(found) | current):
            key = f"{prefix}{instrument_id}"
            difference = found.get(instrument_id, Decimal(0)) - held.get(instrument_id, Decimal(0))
            decimals = (await get_instrument(run.session, instrument_id)).decimals
            if day is None or difference.copy_abs() <= tolerance(decimals):
                await run.void(key)
                continue
            start = (min(day, first) if first is not None else day) - timedelta(days=1)
            await run.post(
                key,
                datetime.combine(start, time(), LONDON),
                OPENING_DESCRIPTION,
                [
                    PostingInput(account_id, instrument_id, difference),
                    PostingInput(opening_balances, instrument_id, -difference),
                ],
            )


# Instruments the ledger does not hold ----------------------------------------------------


async def void_unsupported(run: Run, instrument_ids: Sequence[UUID]) -> None:
    """Void what the connection posted on instruments its market data now says the ledger
    does not hold, and unlink the typed rows that pointed at it."""
    if not instrument_ids:
        return
    t, p = models.Transaction, models.Posting
    keys = sorted(
        k
        for k in await run.session.scalars(
            select(t.external_id)
            .join(p, p.transaction_id == t.id)
            .where(
                t.connection_id == run.connection_id,
                t.status != "void",
                p.instrument_id.in_(instrument_ids),
                _not_opening(),
            )
            .distinct()
        )
        if k is not None
    )
    for key in keys:
        await run.void(key)
    for table in ("etoro_trades", "etoro_position_executions"):
        await run.session.execute(
            text(
                f"update finance.{table} r set transaction_id = null"
                " from finance.transactions t"
                " where t.id = r.transaction_id and r.connection_id = :c"
                " and t.external_id = any(cast(:k as text[]))"
            ),
            {"c": run.connection_id, "k": keys},
        )
