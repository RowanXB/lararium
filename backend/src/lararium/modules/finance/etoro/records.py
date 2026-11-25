"""eToro items that move money or shares, as connector transactions (design §5.7).

**Opens and closes** of the Trading account, one transaction each, keyed `open:<positionId>`
and `close:<positionId>` (Decision 9). A position's open comes from trade history once it has
closed (what closed is what was opened: a partial close's remainder is a position of its own,
with `parentPositionId`, design §6), else from the latest PnL report that lists it, so it
survives the position leaving PnL. An open adds the units at `amount / units` in the account's
currency (`accountCurrency`) and takes `amount` out; a close takes the units out at
`(investment + netProfit) / units` and puts that cash in. The API's fees and taxes are typed,
not posted (design §5.7): an open's execution fees and taxes (orders:lookup; SDRT among them)
and a close's trade-history `fees` (the position's overnight fees and dividends) are what the
statement posts as rows of their own, so posting them here too would count them twice. A
position that is not real (`settlementTypeID` 1, `settlementType` real), has leverage other
than 1, or is copied (a `mirrorID` or `socialTradeId`) is flagged and posts nothing; so does
one no source states the settlement of (counted apart, `settlement_unknown`), and one on an
instrument of a type the ledger does not hold or of no known currency. A run applies the
positions its records name (this sync's PnL, trade history and lookups, the statement rows it
was given, and those the owner has decided on; every one on a replay) and the closes not
posted yet, each from everything stored.

**The statement** (statement_ledger) has a position's "Open Position" and "Position closed"
rows and its Closed Positions row. When the API has the event its values post; else the
statement's (merge.py says which, and opens a review item where they disagree). A position
only the statement has posts from it when Closed Positions says it is real, on the instrument
its ISIN names.

**eToro Money** (key `cash:<id>`): only a `settled` row posts, its `amount` in its currency, a
debit negative. A bank transfer goes through transfer_contra as a bank transfer, naming the
other bank's account when it is a UK one (a GB IBAN, or a sort code and account number), or
giving its account number alone, which the transfer engine reads as an imported row's (design
§5.7); an IBAN outside the UK names nothing and is flagged. An internal transfer names the
Trading account when its counterparty's name says trading (as the owner's data shows), and goes to
Unassigned when it names MoneyFarm or the ISA (design §5.12), or neither, or Trading while the
balances list none (both flagged); a card payment or balance adjustment goes to Unassigned.

**Opening balances** (design §5.5): eToro Money's earliest balance-history snapshot, less
what its records post up to the end of that day, is one opening adjustment per currency,
against Opening balances, the day before its earliest record; every run sets it afresh, so
records arriving later move it, and records reaching back leave none. eToro Trading gets none.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, text

from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.connector_run import (
    OPENING,
    holdings,
    not_opening,
    per_unit,
    settle_rounding,
)
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.etoro import accounts, merge, statement_ledger
from lararium.modules.finance.etoro.merge import Describes, TradeEvent
from lararium.modules.finance.etoro.state import (
    LONDON,
    EtoroDataError,
    Item,
    Record,
    Report,
    Run,
    decimal_text,
    ending,
    flat,
    id_at,
    latest_sighted,
    moment_at,
    number_at,
    number_or_none,
    objects_at,
    record_key,
    records_of,
    text_at,
)
from lararium.modules.finance.identifiers import get_or_create_identifier, normalize_identifier
from lararium.modules.finance.instruments import InstrumentRecord, get_instrument
from lararium.modules.finance.ledger import CounterpartyFields, PostingInput
from lararium.modules.finance.money import to_plain_str, tolerance

OPEN = "open:"
CLOSE = "close:"
OPENING_DESCRIPTION = "Opening balance adjustment"
UNSUPPORTED_POSITION = "a CFD, leveraged or copied position"
SETTLEMENT_UNKNOWN = "position settlement unknown"
UNKNOWN_INTERNAL = "an internal transfer names no account Lararium knows"
NOT_UK = "bank transfer to a non-UK account"
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
    # What the account statement says of it (statement_ledger).
    statement: statement_ledger.Trade | None = None

    @property
    def from_api(self) -> bool:
        return self.pnl is not None or self.trade is not None

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


async def _latest_items(run: Run, endpoint: str) -> dict[str, tuple[Record, Item]]:
    """Each position's item in the record of the latest day whose report of the endpoint
    listed it (its typed rows say which)."""
    table, items = _STORED_ITEMS[endpoint]
    rows = (
        await run.session.execute(
            text(
                f"select distinct on (position_id) position_id::text as position,"
                f" raw_record_id from finance.{table} where connection_id = :c"
                " order by position_id, as_of_date desc"
            ),
            {"c": run.connection_id},
        )
    ).all()
    records = {r.id: r for r in await records_of(run.session, [row.raw_record_id for row in rows])}
    found: dict[str, tuple[Record, Item]] = {}
    for row in rows:
        record = records.get(row.raw_record_id)
        item = items([record]).get(row.position) if record is not None else None
        if item is not None:
            found[row.position] = item
    return found


def _by_execution(lookups: Mapping[str, Record]) -> dict[str, Record]:
    """Each position an order lookup's executions list, with the first lookup listing it."""
    found: dict[str, Record] = {}
    for lookup in lookups.values():
        for item in objects_at(lookup.content, "positionExecutions"):
            found.setdefault(id_at(lookup, item, "positionId"), lookup)
    return found


def _assemble(
    position_id: str,
    pnl: tuple[Record, Item] | None,
    breakdown: tuple[Record, Item] | None,
    trades: Mapping[str, Record],
    lookups: Mapping[str, Record],
    by_execution: Mapping[str, Record],
    statement: statement_ledger.Trade | None = None,
) -> Position:
    """The position with its close and its order's lookup (by the order id its close or PnL
    item gives, else the lookup whose executions list it), and the statement's rows."""
    position = Position(position_id, pnl, breakdown, trades.get(position_id), statement=statement)
    order_id = ""
    if position.trade is not None:
        order_id = text_at(position.trade.content, "orderId")
    elif pnl is not None:
        order_id = text_at(pnl[1], "orderID")
    position.lookup = lookups.get(order_id) or by_execution.get(position_id)
    return position


async def positions(
    run: Run,
    reports: Sequence[Report],
    trades: Mapping[str, Record],
    lookups: Mapping[str, Record],
    given: set[str],
    replay: bool,
    statement: statement_ledger.Statement,
) -> list[Position]:
    """The positions the run applies (module doc). `trades` and `lookups` are every stored
    one (by position and by order id), `given` the position ids this run's trade history,
    lookups and statement rows name. An item this run's reports do not give is the latest
    stored one; a position's statement rows are every stored one."""
    ordered = [r.record for r in sorted(reports, key=lambda r: r.order)]
    pnl = _pnl_items(r for r in ordered if r.endpoint == "pnl")
    breakdown = _breakdown_items(r for r in ordered if r.endpoint == "breakdown")
    wanted = set(pnl) | given
    if replay:
        wanted |= set(trades) | set(statement.trades)
        for lookup in lookups.values():
            wanted |= {
                id_at(lookup, item, "positionId")
                for item in objects_at(lookup.content, "positionExecutions")
            }
    else:
        pnl = await _latest_items(run, "pnl") | pnl
        breakdown = await _latest_items(run, "breakdown") | breakdown
    by_execution = _by_execution(lookups)
    return [
        _assemble(
            p, pnl.get(p), breakdown.get(p), trades, lookups, by_execution, statement.trades.get(p)
        )
        for p in sorted(wanted, key=int)
    ]


type Settlement = Literal["real", "unsupported", "unknown"]


def _settlement(position: Position) -> Settlement:
    """`real` when every source that says so agrees it is real, unleveraged and not copied;
    `unknown` when none states its settlement (eToro's lookup of its order missing, say), so
    it is not taken to be real; else `unsupported`. One with no source stating its leverage
    is not taken to be unleveraged."""
    pnl = position.pnl[1] if position.pnl is not None else None
    breakdown = position.breakdown[1] if position.breakdown is not None else None
    trade = position.trade.content if position.trade is not None else None
    lookup = position.lookup.content if position.lookup is not None else None
    record = position.trade or (position.pnl[0] if position.pnl is not None else None)
    assert record is not None  # a position has a trade or a PnL item (position())

    def numbers(*found: tuple[str, str]) -> list[Decimal]:
        return [decimal_text(record, value, path) for value, path in found if value]

    leverage = numbers(
        *((text_at(i, "leverage"), "leverage") for i in (pnl, breakdown, trade)),
        (text_at(lookup, "asset.leverage"), "asset.leverage"),
    )
    settled = [text_at(pnl, "settlementTypeID") or None]
    settled.append(text_at(breakdown, "settlementType").lower() or None)
    settled.append(text_at(lookup, "asset.settlementType").lower() or None)
    real = [v in ("1", "real") for v in settled if v is not None]
    copied = numbers(
        (text_at(pnl, "mirrorID"), "mirrorID"), (text_at(trade, "socialTradeId"), "socialTradeId")
    )
    if (real and not all(real)) or any(v != 1 for v in leverage) or any(v != 0 for v in copied):
        return "unsupported"
    if not real:
        return "unknown"
    return "real" if leverage else "unsupported"


def settled(position: Position) -> Settlement:
    """The settlement the position posts by: the API's when the API has the position and
    states it (_settlement); else the statement's, when its Closed Positions row states it
    (statement_ledger.settlement). So a close whose order lookup is missing (a 404, or no
    orderId) posts once a statement covers it."""
    statement = position.statement
    found: Settlement = _settlement(position) if position.from_api else "unknown"
    if found == "unknown" and statement is not None:
        found = statement_ledger.settlement(statement)
    return found


def _plain(quantity: Decimal) -> str:
    return to_plain_str(quantity.copy_abs().normalize())


async def _rounding(
    run: Run,
    position_id: str,
    postings: list[PostingInput],
    unassigned: UUID,
    currency: InstrumentRecord,
    quantity: Decimal,
) -> None:
    """The position's rounding residual (connector_run.settle_rounding); more than rounding
    fails the run, naming the position by its last digits."""
    await settle_rounding(
        run.session,
        postings,
        unassigned,
        currency,
        quantity,
        lambda: EtoroDataError(
            f"eToro position {ending(position_id)}: its cash differs from its units times price"
            " by more than rounding"
        ),
    )


async def _account_currency(run: Run) -> InstrumentRecord:
    if not run.account_currency:
        raise EtoroDataError("eToro has not given the Trading account's currency")
    return await run.currency(run.account_currency)


def _api_open(position: Position) -> TradeEvent | None:
    """The API's open: trade history's once the position closed (what closed: the whole
    position when its `investment` is its `initialInvestment`, else a part), else PnL's (the
    position as opened)."""
    describes: Describes
    if position.trade is not None:
        record, item = position.trade, position.trade.content
        occurred_at = moment_at(record, item, "openTimestamp")
        units, amount = number_at(record, item, "units"), number_at(record, item, "investment")
        initial = number_or_none(record, item, "initialInvestment")
        describes = "closed_whole" if initial is not None and initial == amount else "closed_part"
    elif position.pnl is not None:
        record, item = position.pnl
        occurred_at = moment_at(record, item, "openDateTime")
        units, amount = number_at(record, item, "units"), number_at(record, item, "amount")
        describes = "opened"
    else:
        return None
    return TradeEvent(
        units.copy_abs(),
        amount.copy_abs(),
        None,
        local_date(occurred_at, LONDON),
        occurred_at,
        describes,
    )


def _api_close(position: Position) -> TradeEvent | None:
    """The API's close: trade history's `investment` + `netProfit`. Its `fees` are typed, not
    posted (module doc)."""
    if position.trade is None:
        return None
    record, item = position.trade, position.trade.content
    profit = number_at(record, item, "netProfit")
    occurred_at = moment_at(record, item, "closeTimestamp")
    return TradeEvent(
        number_at(record, item, "units").copy_abs(),
        number_at(record, item, "investment") + profit,
        profit,
        local_date(occurred_at, LONDON),
        occurred_at,
        statement_ledger.CLOSE,
    )


async def _post_open(run: Run, position: Position, instrument_id: UUID) -> UUID | None:
    key = f"{OPEN}{position.position_id}"
    api = _api_open(position)
    statement = statement_ledger.open_event(position.statement, position.position_id)
    if api is None and statement is None:
        return None
    choice = await merge.choose(run, key, api, statement)
    values = choice.values
    units, amount = values.units, values.cash
    if units == 0:
        raise EtoroDataError(f"eToro position {ending(position.position_id)} has no units")
    currency = await _account_currency(run)
    unassigned = await run.system_account("unassigned")
    postings = [
        PostingInput(
            run.trading,
            instrument_id,
            units,
            cost_per_unit=per_unit(amount, units),
            cost_instrument_id=currency.id,
        )
    ]
    if amount != 0:
        postings.append(PostingInput(run.trading, currency.id, -amount))
    await _rounding(run, position.position_id, postings, unassigned, currency, units)
    description = f"Buy {_plain(units)} {await run.symbol(instrument_id)}"
    transaction_id = await run.post(key, values.occurred_at, description, postings)
    await merge.review(run, key, transaction_id, choice)
    return transaction_id


async def _post_close(run: Run, position: Position, instrument_id: UUID) -> UUID | None:
    key = f"{CLOSE}{position.position_id}"
    api = _api_close(position)
    statement = statement_ledger.close_event(position.statement, position.position_id)
    if api is None and statement is None:
        return None
    choice = await merge.choose(run, key, api, statement)
    values = choice.values
    units, proceeds = values.units, values.cash
    if units == 0:
        raise EtoroDataError(f"eToro position {ending(position.position_id)} has no units")
    if proceeds < 0:
        raise EtoroDataError(
            f"eToro position {ending(position.position_id)} closed for less than 0"
        )
    currency = await _account_currency(run)
    unassigned = await run.system_account("unassigned")
    postings = [
        PostingInput(
            run.trading,
            instrument_id,
            -units,
            price_per_unit=per_unit(proceeds, units),
            price_instrument_id=currency.id,
        )
    ]
    if proceeds != 0:
        postings.append(PostingInput(run.trading, currency.id, proceeds))
    await _rounding(run, position.position_id, postings, unassigned, currency, units)
    description = f"Sell {_plain(units)} {await run.symbol(instrument_id)}"
    transaction_id = await run.post(key, values.occurred_at, description, postings)
    await merge.review(run, key, transaction_id, choice)
    return transaction_id


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


def etoro_instrument(position: Position) -> str:
    """eToro's id of the position's instrument, as the API gives it; empty without one."""
    source = position.trade.content if position.trade is not None else None
    etoro_id = text_at(source, "instrumentId") if source is not None else ""
    if not etoro_id and position.pnl is not None:
        etoro_id = text_at(position.pnl[1], "instrumentID")
    return etoro_id


async def _instrument(run: Run, position: Position) -> UUID | None:
    """The position's instrument when it posts (`settled`): the API's instrument when the API
    has the position, else the statement's (statement_ledger)."""
    statement = position.statement
    if not position.from_api and statement is None:
        return None
    settlement = settled(position)
    if settlement == "unsupported":
        run.flag(UNSUPPORTED_POSITION)
    elif settlement == "real":
        if position.from_api:
            return await run.instrument(etoro_instrument(position))
        assert statement is not None
        return await statement_ledger.instrument(run, position.position_id, statement)
    return None


async def position(run: Run, position: Position) -> None:
    """Post (or void) the position's open and close, and type its close."""
    key = position.position_id
    statement = position.statement
    if not position.from_api and (
        statement is None or (statement.open, statement.close, statement.closed) == (None,) * 3
    ):
        return  # a lookup alone says nothing to post
    instrument_id = await _instrument(run, position)
    opened = closed = None
    if instrument_id is not None:
        opened = await _post_open(run, position, instrument_id)
        closed = await _post_close(run, position, instrument_id)
    if opened is None:
        await run.void(f"{OPEN}{key}")
    if closed is None:
        await run.void(f"{CLOSE}{key}")
    await link_open(run, key, opened)
    if position.trade is not None:
        await trade_row(run, position.trade, closed, instrument_id)
    if statement is not None and statement.closed is not None:
        await statement_ledger.closed_row(run, statement.closed, key, closed, instrument_id)


async def unplaced(
    run: Run,
    trades: Mapping[str, Record],
    pnl: Mapping[str, tuple[Record, Item]],
    statement: statement_ledger.Statement,
) -> int:
    """How many positions only the statement has cannot be placed (design §6): no Closed
    Positions row to say they are real (an open the statement does not close), or one that
    does not say which instrument. Found in full from everything stored, so the flag clears
    once the API or a later statement places them. An open the statement does not close,
    after the latest sync's fetch, is not counted: that sync could not have brought it, the
    next is expected to. `pnl`: each position's latest stored PnL item (latest_pnl)."""
    latest = await latest_sighted(run.session, run.connection_id, "balances")
    fetched = latest.seen_at if latest is not None else None
    count = 0
    for position_id, trade in statement.trades.items():
        if position_id in trades or position_id in pnl:
            continue  # the API has it
        if (trade.open, trade.close, trade.closed) == (None, None, None):
            continue
        settlement = statement_ledger.settlement(trade)
        if settlement == "unsupported":
            continue  # flagged as such
        if settlement == "real":
            if (
                not statement_ledger.placeable(trade)
                and await statement_ledger.posted_instrument(run, position_id) is None
            ):
                count += 1
            continue
        opened = statement_ledger.moment(trade.open, "Date") if trade.open else None
        if trade.close is None and opened is not None and (fetched is None or opened > fetched):
            continue  # the API is expected to bring it
        count += 1
    return count


async def latest_pnl(run: Run) -> dict[str, tuple[Record, Item]]:
    """Each position's item in the latest PnL report that listed it."""
    return await _latest_items(run, "pnl")


async def settlement_unknown(
    run: Run,
    trades: Mapping[str, Record],
    lookups: Mapping[str, Record],
    pnl: Mapping[str, tuple[Record, Item]],
    statement: Mapping[str, statement_ledger.Trade],
) -> int:
    """How many of the connection's positions (closed, or listed in PnL) are `unknown` as
    `settled` reads them, from everything stored. Found in full, so the flag clears once a
    source states the settlement. `pnl`: as latest_pnl; `statement`: the statement's trades
    by position."""
    breakdown = await _latest_items(run, "breakdown")
    by_execution = _by_execution(lookups)
    return sum(
        settled(
            _assemble(
                p, pnl.get(p), breakdown.get(p), trades, lookups, by_execution, statement.get(p)
            )
        )
        == "unknown"
        for p in set(trades) | set(pnl)
    )


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


_ACCOUNT_NUMBER = re.compile(r"[0-9]{8}")


@dataclass(frozen=True)
class _Bank:
    """What a bank transfer's identifiers say of the other bank account: a UK one in the
    registry's form, or its account number alone (design §5.7); `abroad` for an IBAN outside
    the UK."""

    uk_bank_account: str | None = None
    account_number: str | None = None
    abroad: bool = False


def _bank(identifiers: Sequence[Item]) -> _Bank:
    by_name = {text_at(i, "name").casefold(): text_at(i, "value") for i in identifiers}
    iban = re.sub(r"\s", "", by_name.get("iban", "")).upper()
    if iban:
        found = _UK_IBAN.fullmatch(iban)
        return _Bank(f"{found[1]}:{found[2]}") if found else _Bank(abroad=True)
    number = re.sub(
        r"[\s-]", "", by_name.get("bankaccountnumber", "") or by_name.get("accountnumber", "")
    )
    sort_code = by_name.get("sortcode", "")
    if sort_code and number:
        try:
            return _Bank(normalize_identifier("uk_bank_account", f"{sort_code}:{number}"))
        except FinanceValidationError:
            return _Bank()
    if _ACCOUNT_NUMBER.fullmatch(number):
        return _Bank(account_number=number)
    return _Bank()


async def _counterparty(run: Run, record: Record, kind: str) -> tuple[CounterpartyFields, bool]:
    """What the row says about the other side; and whether it goes through transfer_contra
    (a movement with another account) rather than to Unassigned."""
    content = record.content
    name = text_at(content, "counterparty.name") or None
    if kind == "bankTransfer":
        bank = _bank(objects_at(content, "bankTransferTransactionDetails.bankIdentifier"))
        if bank.abroad:
            run.flag(NOT_UK)
        identifier = None
        if (value := bank.uk_bank_account) is not None:
            identifier = await run.shared(
                ("identifier", "uk_bank_account", value),
                lambda cmd: get_or_create_identifier(cmd, "uk_bank_account", value),
            )
        reference = text_at(content, "bankTransferTransactionDetails.paymentReference") or None
        fields = CounterpartyFields(
            identifier, name, reference, bank_transfer=True, account_number=bank.account_number
        )
        return fields, True
    if kind == "internalTransfer":
        trading = _TRADING.search(name or "") is not None
        moneyfarm = _MONEYFARM.search(name or "") is not None
        identifier = None
        if trading and not moneyfarm:
            identifier = await accounts.trading_identifier(run)
        if identifier is None and not (moneyfarm and not trading):
            # Neither account named, both, or Trading while the balances list none.
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
                await run.contra(key, account_id, counterparty.identifier_id)
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
                run.session, account_id, end_of(earliest.snapshot_date), not_opening()
            )
        first = await run.session.scalar(
            select(func.min(t.booking_date))
            .join(p, p.transaction_id == t.id)
            .where(p.account_id == account_id, live, not_opening())
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
                not_opening(),
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
