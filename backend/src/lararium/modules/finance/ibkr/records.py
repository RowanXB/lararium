"""IBKR rows that move money or securities, as connector transactions (design 5.7).

Every row is one transaction keyed by its transactionID. Its postings on the IBKR account come
first; fees and taxes are postings of their own on the account (`is_fee`), with one Unassigned
posting per currency as their counterpart, as Monzo's fees are. A cash movement or transfer
has exactly two postings, the contra at position 1, so M4 can match it.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ibkr import tables
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.state import Item, Run, zone_name
from lararium.modules.finance.identifiers import get_or_create_identifier, normalize_identifier
from lararium.modules.finance.instruments import InstrumentRecord, get_instrument
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    compute_residuals,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.transfers import transfer_contra

# The external id of each transactional record type: <prefix>:<accountId>:<transactionID>.
# Decision 9 had <prefix>:<transactionID>, but IBKR gives both sides of an internal transfer
# the same transactionID, one in each account (real statements, 2026-11-19), and a connection
# holds both accounts; ibkr_* rows are unique per account and transactionID likewise.
PREFIXES: Mapping[str, str] = {
    "ibkr.trade": "trade",
    "ibkr.cash_transaction": "cash",
    "ibkr.corporate_action": "ca",
    "ibkr.transfer": "transfer",
}
OPENING_DESCRIPTION = "Opening balance adjustment"
_OPENING = "opening:"
# The container element of Open Positions; a statement lists it even when it is empty.
POSITIONS_SECTION = "OpenPositions"
_DEPOSITS = "Deposits/Withdrawals"
_CASH = "CASH"

type Upsert = Callable[[CommandContext, tables.Fields, tables.Fields], Awaitable[bool]]


def external_id(item: Item) -> str:
    return f"{PREFIXES[item.record_type]}:{item.account}:{item.text('transactionID')}"


def shown(item: Item) -> str:
    """The row as an error shows it: its transactionID and the last four digits of its
    account, never the full account id."""
    return f"{item.text('transactionID')} of ···{item.account[-4:]}"


def order(item: Item) -> tuple[str, str]:
    """By report date, then transaction id (numerically, as IBKR's ids are digits)."""
    return item.text("reportDate"), item.text("transactionID").rjust(40, "0")


async def _post(
    run: Run,
    item: Item,
    *,
    description: str,
    postings: Sequence[PostingInput],
    value_date: date | None,
    counterparty: CounterpartyFields | None = None,
) -> UUID:
    moment = item.moment()
    record, outcome = await upsert_connector_transaction(
        run.command,
        TransactionInput(
            occurred_at=moment,
            occurred_tz=zone_name(moment),
            booking_date=item.day("reportDate"),
            value_date=value_date,
            description=description,
            postings=postings,
            origin="connector",
            connection_id=run.connection_id,
            external_id=external_id(item),
            counterparty=counterparty or CounterpartyFields(),
        ),
    )
    run.count(outcome)
    return record.id


async def _typed(
    run: Run,
    upsert: Upsert,
    table: str,
    item: Item,
    transaction_id: UUID,
    account_id: UUID,
    extra: Mapping[str, object] | None = None,
) -> None:
    values = await tables.typed_values(run.session, table, item.section, item.attributes)
    values |= {"account_id": account_id, **(extra or {})}
    await upsert(run.command, {"transaction_id": transaction_id}, values)


def _fees(
    account_id: UUID, unassigned: UUID, fees: Iterable[tuple[UUID, Decimal, str]]
) -> list[PostingInput]:
    """Each non-zero fee on the account, then Unassigned's counterpart per currency."""
    postings: list[PostingInput] = []
    totals: dict[UUID, Decimal] = {}
    for instrument_id, amount, memo in fees:
        if amount == 0:
            continue
        postings.append(PostingInput(account_id, instrument_id, amount, is_fee=True, memo=memo))
        totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + amount
    postings += [
        PostingInput(unassigned, instrument_id, -total)
        for instrument_id, total in totals.items()
        if total != 0
    ]
    return postings


async def _rounding(
    run: Run,
    item: Item,
    postings: list[PostingInput],
    unassigned: UUID,
    currency: InstrumentRecord,
    quantity: Decimal,
    price: Decimal,
) -> None:
    """IBKR rounds proceeds to cents and may round tradePrice. A residual within the ledger's
    tolerance stays; one beyond it is posted to Unassigned (memo "rounding", design 5.7), up
    to what rounding can explain: |quantity| times half a unit of tradePrice's last decimal
    place, plus the currency's tolerance. More than that (proceeds in pence against a price
    in pounds, say) fails the sync, naming the row's key but no amount."""
    allowed = tolerance(currency.decimals)
    if price != 0:
        exponent = price.as_tuple().exponent
        assert isinstance(exponent, int)  # finite
        allowed += quantity.copy_abs() * Decimal(1).scaleb(exponent) / 2
    for instrument_id, residual in compute_residuals(postings).items():
        decimals = (await get_instrument(run.session, instrument_id)).decimals
        if residual.copy_abs() <= tolerance(decimals):
            continue
        if instrument_id != currency.id or residual.copy_abs() > allowed:
            raise FlexDataError(
                f"{item.section} {shown(item)}: its proceeds differ from quantity times"
                " tradePrice by more than rounding"
            )
        postings.append(PostingInput(unassigned, instrument_id, -residual, memo="rounding"))


async def trade(run: Run, item: Item) -> None:
    account_id = run.account_id(item)
    currency = await run.currency(item.text("currency"))
    quantity = item.number("quantity")
    price = item.number("tradePrice")
    symbol = item.text("symbol")
    conversion = item.text("assetCategory") == _CASH
    if conversion:
        # BASE.QUOTE: quantity of the base currency, priced in the quote (the trade currency).
        base, _, quote = symbol.partition(".")
        if quote != currency.symbol:
            raise FlexDataError("Trade.symbol of a currency conversion does not name its currency")
        instrument_id = (await run.currency(base)).id
    else:
        found = await run.instrument(item)
        if found is None:
            return
        instrument_id = found
    unassigned = await run.system_account("unassigned")
    postings: list[PostingInput] = []
    if quantity != 0:
        if price == 0:
            # Nothing to weigh it by: the security comes from (or goes to) Unassigned, as in
            # a corporate action, and the proceeds must then be within tolerance of zero.
            postings.append(PostingInput(account_id, instrument_id, quantity))
            postings.append(PostingInput(unassigned, instrument_id, -quantity))
        elif conversion or quantity < 0:
            # Currencies have no lots, and a sell is weighed by its price: realised P/L is a
            # typed field, not a posting.
            postings.append(
                PostingInput(
                    account_id,
                    instrument_id,
                    quantity,
                    price_per_unit=price,
                    price_instrument_id=currency.id,
                )
            )
        else:
            postings.append(
                PostingInput(
                    account_id,
                    instrument_id,
                    quantity,
                    cost_per_unit=price,
                    cost_instrument_id=currency.id,
                )
            )
    proceeds = item.number("proceeds")
    if proceeds != 0:
        postings.append(PostingInput(account_id, currency.id, proceeds))
    commission = item.number("ibCommission")
    commission_currency = (
        (await run.currency(item.text("ibCommissionCurrency"))).id if commission else currency.id
    )
    postings += _fees(
        account_id,
        unassigned,
        [
            (commission_currency, commission, "commission"),
            (currency.id, item.number("taxes"), "taxes"),
        ],
    )
    if not postings:
        run.flag("Trade.unposted")
        return
    await _rounding(run, item, postings, unassigned, currency, quantity, price)
    side = "Buy" if quantity > 0 else "Sell" if quantity < 0 else ""
    description = f"{side} {to_plain_str(abs(quantity))} {symbol}" if side else symbol
    transaction_id = await _post(
        run,
        item,
        description=description,
        postings=postings,
        value_date=item.optional_day("settleDateTarget"),
    )
    await _typed(run, tables.upsert_trades, "ibkr_trades", item, transaction_id, account_id)


async def _transfer_contra(
    run: Run, item: Item, account_id: UUID, identifier_id: UUID | None
) -> UUID:
    # Both system accounts must exist for transfer_contra.
    await run.system_account("unassigned")
    await run.system_account("in_transit")
    return await transfer_contra(
        run.session,
        source=run.source(external_id(item)),
        own_account_id=account_id,
        counterparty_identifier_id=identifier_id,
        other_postings=0,
    )


async def cash_transaction(run: Run, item: Item) -> None:
    account_id = run.account_id(item)
    currency = await run.currency(item.text("currency"))
    amount = item.number("amount")
    if amount == 0:
        run.flag("CashTransaction.unposted")
        return
    kind = item.text("type")
    if kind == _DEPOSITS:
        # Every one is posted: IBKR lists internal movements in Transfers only (design 5.7).
        # A movement with an outside bank: the deposit that pairs with Monzo (design 5.12).
        counterparty = CounterpartyFields(bank_transfer=True)
        contra = await _transfer_contra(run, item, account_id, None)
    else:
        counterparty = CounterpartyFields()
        contra = await run.system_account("unassigned")
    related = (
        await run.instrument(item, flag=False)
        if item.text("conid") and item.text("assetCategory") not in ("", _CASH)
        else None
    )
    transaction_id = await _post(
        run,
        item,
        description=item.text("description") or kind,
        postings=[
            PostingInput(account_id, currency.id, amount),
            PostingInput(contra, currency.id, -amount),
        ],
        value_date=item.optional_day("settleDate"),
        counterparty=counterparty,
    )
    await _typed(
        run,
        tables.upsert_cash_transactions,
        "ibkr_cash_transactions",
        item,
        transaction_id,
        account_id,
        {"instrument_id": related},
    )


async def corporate_action(run: Run, item: Item) -> None:
    account_id = run.account_id(item)
    instrument_id = await run.instrument(item)
    if instrument_id is None:
        return
    currency = await run.currency(item.text("currency"))
    moves = [
        (instrument_id, item.number("quantity")),
        (currency.id, item.number("proceeds")),
    ]
    moves = [(i, amount) for i, amount in moves if amount != 0]
    if not moves:
        run.flag("CorporateAction.unposted")
        return
    unassigned = await run.system_account("unassigned")
    # A split, spin-off or merger is quantity from or to Unassigned (equity).
    postings = [PostingInput(account_id, i, amount) for i, amount in moves] + [
        PostingInput(unassigned, i, -amount) for i, amount in moves
    ]
    transaction_id = await _post(
        run,
        item,
        description=item.text("description")
        or item.text("actionDescription")
        or item.text("type")
        or "Corporate action",
        postings=postings,
        value_date=None,
    )
    await _typed(
        run,
        tables.upsert_corporate_actions,
        "ibkr_corporate_actions",
        item,
        transaction_id,
        account_id,
    )


async def _other_account(run: Run, value: str) -> UUID | None:
    """The registry row of the IBKR account a transfer names; None when it names none, or
    something that is not an IBKR account id (another broker's account)."""
    if not value:
        return None
    try:
        normalized = normalize_identifier("ibkr_account", value)
    except FinanceValidationError:
        return None
    return await run.shared(
        ("identifier", "ibkr_account", normalized),
        lambda cmd: get_or_create_identifier(cmd, "ibkr_account", normalized),
    )


async def transfer(run: Run, item: Item) -> None:
    account_id = run.account_id(item)
    if item.text("assetCategory") == _CASH:
        instrument_id = (await run.currency(item.text("currency"))).id
        amount = item.number("cashTransfer")
    else:
        found = await run.instrument(item)
        if found is None:
            return
        instrument_id = found
        # A security's quantity is signed by direction; cashTransfer carries its own sign.
        direction = item.text("direction").upper()
        if direction not in ("IN", "OUT"):
            raise FlexDataError("Transfer.direction is not IN or OUT")
        amount = item.number("quantity").copy_abs()
        amount = -amount if direction == "OUT" else amount
    if amount == 0:
        run.flag("Transfer.unposted")
        return
    identifier_id = await _other_account(run, item.text("account"))
    contra = await _transfer_contra(run, item, account_id, identifier_id)
    # No cost: the In transit leg balances by quantity. The transfer price and position
    # amount, the cost basis for tax, are typed fields.
    transaction_id = await _post(
        run,
        item,
        description=item.text("description") or "Transfer",
        postings=[
            PostingInput(account_id, instrument_id, amount),
            PostingInput(contra, instrument_id, -amount),
        ],
        value_date=item.optional_day("settleDate"),
        counterparty=CounterpartyFields(identifier_id=identifier_id),
    )
    await _typed(run, tables.upsert_transfers, "ibkr_transfers", item, transaction_id, account_id)


# Openings ------------------------------------------------------------------------------------


def _not_opening() -> ColumnElement[bool]:
    t = models.Transaction
    return func.coalesce(t.external_id, "").not_like(f"{_OPENING}%")


async def _held(
    run: Run,
    account_id: UUID,
    instrument_id: UUID,
    *,
    before: date | None = None,
    through: date | None = None,
) -> Decimal:
    """The account's non-void postings in the instrument, openings left out."""
    t, p = models.Transaction, models.Posting
    query = (
        select(func.coalesce(func.sum(p.quantity), 0))
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            p.instrument_id == instrument_id,
            t.status != "void",
            _not_opening(),
        )
    )
    if before is not None:
        query = query.where(t.booking_date < before)
    if through is not None:
        query = query.where(t.booking_date <= through)
    return Decimal(await run.session.scalar(query) or 0)


async def _securities_posted(run: Run, account_id: UUID) -> set[UUID]:
    t, p, i = models.Transaction, models.Posting, models.Instrument
    rows = await run.session.scalars(
        select(p.instrument_id)
        .distinct()
        .join(t, t.id == p.transaction_id)
        .join(i, i.id == p.instrument_id)
        .where(p.account_id == account_id, t.status != "void", i.kind != "fiat", _not_opening())
    )
    return set(rows)


async def _earliest_booking(run: Run, account_id: UUID) -> date | None:
    t, p = models.Transaction, models.Posting
    return await run.session.scalar(
        select(func.min(t.booking_date))
        .join(p, p.transaction_id == t.id)
        .where(p.account_id == account_id, t.status != "void", _not_opening())
    )


async def _opening(
    run: Run, ibkr_id: str, account_id: UUID, instrument_id: UUID, difference: Decimal, day: date
) -> None:
    key = f"{_OPENING}{ibkr_id}:{instrument_id}"
    if difference == 0:
        t = models.Transaction
        posted = await run.session.scalar(
            select(t.id).where(
                t.connection_id == run.connection_id, t.external_id == key, t.status != "void"
            )
        )
        if posted is not None:
            await void_transaction(run.command, posted)
        return
    occurred_at = datetime.combine(day, time(), tables.NEW_YORK)
    _, outcome = await upsert_connector_transaction(
        run.command,
        TransactionInput(
            occurred_at=occurred_at,
            occurred_tz=zone_name(occurred_at),
            booking_date=day,
            description=OPENING_DESCRIPTION,
            postings=[
                PostingInput(account_id, instrument_id, difference),
                PostingInput(
                    await run.system_account("opening_balances"), instrument_id, -difference
                ),
            ],
            origin="connector",
            connection_id=run.connection_id,
            external_id=key,
        ),
    )
    run.count(outcome)


async def openings(
    run: Run,
    items: Sequence[Item],
    opened: Mapping[str, date],
    *,
    cash_report: str,
    open_position: str,
    is_summary: Callable[[Item], bool],
    sections: frozenset[tuple[str, str]],
) -> None:
    """For an account older than the window, what it held before it (design 5.7): cash from
    the earliest Cash Report's startingCash, positions from the window's last reported ones
    less what the window posted; dated the day before the account's earliest record, against
    Opening balances. The caller runs it for the account's earliest window only."""
    by_account: defaultdict[str, list[Item]] = defaultdict(list)
    for item in items:
        by_account[item.account].append(item)
    for ibkr_id, rows in sorted(by_account.items()):
        start = min(row.period_from for row in rows)
        if ibkr_id not in opened or opened[ibkr_id] >= start or ibkr_id not in run.accounts:
            continue
        account_id = run.accounts[ibkr_id]
        targets: dict[UUID, Decimal] = {}

        earliest: dict[str, Item] = {}
        for row in rows:
            if row.record_type != cash_report or row.text("currency") == "BASE_SUMMARY":
                continue
            code = row.text("currency")
            if code not in earliest or row.day("fromDate") < earliest[code].day("fromDate"):
                earliest[code] = row
        for code, row in sorted(earliest.items()):
            currency = await run.currency(code)
            held = await _held(run, account_id, currency.id, before=row.day("fromDate"))
            targets[currency.id] = row.number("startingCash") - held

        if (ibkr_id, POSITIONS_SECTION) not in sections:
            # A window without the section says nothing about positions (an empty one says
            # there were none): its securities get no opening.
            run.flag(POSITIONS_SECTION)
            positions_known = False
        else:
            positions_known = True
        positions = [r for r in rows if r.record_type == open_position and is_summary(r)]
        # The last day positions were reported for (a complete list of that day), else the
        # window's end, when the window reported none.
        last = max(
            (r.day("reportDate") for r in positions),
            default=max(row.period_to for row in rows),
        )
        reported: defaultdict[UUID, Decimal] = defaultdict(Decimal)
        for row in positions:
            if row.day("reportDate") == last:
                instrument_id = await run.instrument(row, flag=False)
                if instrument_id is not None:
                    reported[instrument_id] += row.number("position")
        securities = set(reported) | await _securities_posted(run, account_id)
        for instrument_id in sorted(securities) if positions_known else ():
            held = await _held(run, account_id, instrument_id, through=last)
            targets[instrument_id] = reported.get(instrument_id, Decimal(0)) - held

        first = await _earliest_booking(run, account_id)
        day = min(start, first) if first is not None else start
        for instrument_id, difference in targets.items():
            await _opening(
                run, ibkr_id, account_id, instrument_id, difference, day - timedelta(days=1)
            )
