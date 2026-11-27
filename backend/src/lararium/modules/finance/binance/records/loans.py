"""Crypto loans (design 5.4, 5.6, 5.7): the debt is a negative holding of the loan coin on the
Crypto Loans account (`liability`), whose report is minus the ongoing orders' `totalDebt`.

- A flexible borrow (`SUCCESS`) moves the loan coin to Spot against the debt: `+initialLoanAmount`
  on Spot, `-` on Crypto Loans. A repay (`REPAID`) is the reverse for the whole `repayAmount`,
  which Binance does not split into principal and interest.
- A flexible loan's interest is added to its debt, and Binance lists it nowhere: it is derived
  from the reports (`set_interest`), as the opening is.
- A VIP repay (`Repaid`) is the reverse of a borrow for its `repayAmount`, and its
  `repayInterest` is a fee on the loan. Its `totalDebt` is taken as the principal: interest
  counts when it is paid, so the accrued interest history is typed only. A VIP loan has no
  borrow history: its debt before the first report is in the opening.
- A liquidation (`Liquidated`) sells collateral to repay the debt: a swap of the collateral,
  which a flexible loan holds as a Simple Earn flexible position, from Earn for the debt repaid
  on Crypto Loans. Its fee and the collateral returned are typed only.
- Collateral stays where Binance reports it (pledged from Spot, it is subscribed to Simple Earn
  flexible, type `LOAN`): LTV adjustments, VIP requests and collateral accounts are typed only.

None of these rows has an id, and a row's status changes as it completes: its ledger key is
what the catalogue identifies it by and its occurrence, not its content, so a row seen pending
and then finished is one movement with one typed row, the version applied last (`_supersede`).
Every record is marked never a transfer (`records.post`).
"""

import bisect
import itertools
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, not_, select
from sqlalchemy import text as sql

from lararium.modules.finance import models
from lararium.modules.finance.binance import layout, tables
from lararium.modules.finance.binance.parse import Row
from lararium.modules.finance.binance.reconcile import asset_code, report_times, wallets
from lararium.modules.finance.binance.records import (
    Run,
    amount,
    code,
    fees,
    post,
    remove,
    text,
    unit_cost,
    when,
)
from lararium.modules.finance.ledger import PostingInput
from lararium.modules.finance.money import to_plain_str, tolerance

BORROWS = "loan-flexible-borrows"
REPAYS = "loan-flexible-repays"
LIQUIDATIONS = "loan-flexible-liquidations"
VIP_REPAYS = "vip-loan-repays"
FLEXIBLE_ORDERS = "loan-flexible-ongoing"
LOANS = "loans"
INTEREST_PREFIX = "loan_interest:"
INTEREST_MEMO = "loan interest"
_BORROWED = "SUCCESS"
_REPAID = "REPAID"
_VIP_REPAID = "Repaid"
# The catalogue lists no enum: "Liquidating" is the field's description, "Liquidated" its
# example.
_LIQUIDATED = "Liquidated"
_LIQUIDATION_STATUSES = frozenset({"Liquidating", _LIQUIDATED})
# The flexible histories a loan coin's movements are in, and their time columns: a coin with one
# of them inside an interval had a flexible loan then.
_FLEXIBLE_HISTORIES = {
    BORROWS: "borrow_time",
    REPAYS: "repay_time",
    LIQUIDATIONS: "liquidation_starting_time",
}


def _plain(quantity: Decimal) -> str:
    return to_plain_str(quantity.normalize())


def _key(run: Run, *parts: str) -> str:
    """`<record>:<parts>:<occurrence>`: the same for every status the row is seen in."""
    record = run.record.key.split(":", 1)[0]
    occurrence = run.record.typed_key[layout.OCCURRENCE]
    return ":".join([record, *parts, str(occurrence)])


async def _supersede(run: Run, label: str, identity: Mapping[str, object]) -> None:
    """Delete the typed rows of the same movement (the columns of `identity`, the occurrence)
    in another version (another status): Binance lists one movement, which it has updated."""
    key = run.record.typed_key
    where = " and ".join(f"{column} = :v{i}" for i, column in enumerate(identity))
    await run.session.execute(
        sql(
            f"delete from finance.{layout.table_name(label)}"
            f" where connection_id = :c and {where} and occurrence = :o and content_hash <> :h"
        ),
        {
            "c": key["connection_id"],
            "o": key[layout.OCCURRENCE],
            "h": key[layout.CONTENT_HASH],
            **{f"v{i}": value for i, value in enumerate(identity.values())},
        },
    )


async def _unposted(run: Run, key: str) -> None:
    await remove(run, key)
    await run.typed(transaction_id=None)


async def _move(
    run: Run,
    key: str,
    *,
    coin: str,
    quantity: Decimal,
    at: datetime,
    description: str,
    interest: Decimal = Decimal(0),
) -> None:
    """`quantity` of the coin onto Spot against the debt (negative: back from Spot to the loan),
    and `interest` as a fee on the loan."""
    instrument = await run.asset(coin)
    if instrument is None:
        await _unposted(run, key)
        return
    spot, loans = await run.account("spot"), await run.account(LOANS)
    postings = [
        PostingInput(spot, instrument, quantity),
        PostingInput(loans, instrument, -quantity),
        *fees(loans, run.unassigned, [(instrument, interest, INTEREST_MEMO)]),
    ]
    transaction_id = await post(
        run, key, occurred_at=at, description=f"{description} {coin}", postings=postings
    )
    await run.typed(transaction_id=transaction_id)


async def borrow(run: Run, row: Row) -> None:
    """A flexible borrow: `+initialLoanAmount` on Spot, `-` on Crypto Loans, once `SUCCESS`."""
    coin, collateral = text(row, "loanCoin"), text(row, "collateralCoin")
    quantity = amount(BORROWS, row, "initialLoanAmount")
    at = when(BORROWS, row, "borrowTime")
    if coin is None or collateral is None or quantity is None or at is None:
        return
    key = _key(run, coin, collateral, str(tables.millis(at)), _plain(quantity))
    identity = {
        "loan_coin": coin,
        "collateral_coin": collateral,
        "borrow_time": at,
        "initial_loan_amount": quantity,
    }
    await _supersede(run, BORROWS, identity)
    if text(row, "status") != _BORROWED:
        # Pending or failed: nothing moved yet.
        await _unposted(run, key)
        return
    await _move(run, key, coin=coin, quantity=quantity, at=at, description="Crypto loan of")


async def repay(run: Run, row: Row) -> None:
    """A flexible repay: `-repayAmount` on Spot, `+` on Crypto Loans, once `REPAID`."""
    coin, collateral = text(row, "loanCoin"), text(row, "collateralCoin")
    quantity = amount(REPAYS, row, "repayAmount")
    at = when(REPAYS, row, "repayTime")
    if coin is None or collateral is None or quantity is None or at is None:
        return
    key = _key(run, coin, collateral, str(tables.millis(at)), _plain(quantity))
    identity = {
        "loan_coin": coin,
        "collateral_coin": collateral,
        "repay_time": at,
        "repay_amount": quantity,
    }
    await _supersede(run, REPAYS, identity)
    if text(row, "repayStatus") != _REPAID:
        await _unposted(run, key)
        return
    await _move(run, key, coin=coin, quantity=-quantity, at=at, description="Crypto loan repay of")


async def vip_repay(run: Run, row: Row) -> None:
    """A VIP repay: `-repayAmount` on Spot, `+` on Crypto Loans, and `repayInterest` as a fee
    on the loan, once `Repaid`."""
    coin, order = text(row, "loanCoin"), text(row, "orderId")
    quantity = amount(VIP_REPAYS, row, "repayAmount")
    at = when(VIP_REPAYS, row, "repayTime")
    if coin is None or quantity is None or at is None:
        return
    if order is None:
        run.flag(f"{VIP_REPAYS}.orderId missing")
        return
    key = _key(run, order, str(tables.millis(at)))
    identity = {"order_id": order, "repay_time_at": at}
    await _supersede(run, VIP_REPAYS, identity)
    if text(row, "repayStatus") != _VIP_REPAID:
        await _unposted(run, key)
        return
    interest = amount(VIP_REPAYS, row, "repayInterest") or Decimal(0)
    await _move(
        run,
        key,
        coin=coin,
        quantity=-quantity,
        at=at,
        description="VIP loan repay of",
        interest=interest,
    )


async def liquidation(run: Run, row: Row) -> None:
    """A flexible liquidation, once `Liquidated`: `-liquidationCollateralAmount` of the
    collateral on Earn, `+liquidationDebt` of the loan coin on Crypto Loans weighed by its cost
    in the collateral."""
    coin, collateral = text(row, "loanCoin"), text(row, "collateralCoin")
    at = when(LIQUIDATIONS, row, "liquidationStartingTime")
    debt = amount(LIQUIDATIONS, row, "liquidationDebt")
    sold = amount(LIQUIDATIONS, row, "liquidationCollateralAmount")
    if coin is None or collateral is None or at is None:
        return
    status = text(row, "status")
    if status not in _LIQUIDATION_STATUSES:
        run.flag(f"{LIQUIDATIONS}.status={code(status)}")
    if debt is None or sold is None:
        # Typed only. Only a finished liquidation needs its amounts: one still in progress
        # without them is not posted yet, and says nothing wrong.
        if status == _LIQUIDATED:
            run.flag(f"{LIQUIDATIONS}.liquidationDebt or liquidationCollateralAmount missing")
        return
    key = _key(run, coin, collateral, str(tables.millis(at)), _plain(debt))
    identity = {
        "loan_coin": coin,
        "collateral_coin": collateral,
        "liquidation_starting_time": at,
        "liquidation_debt": debt,
    }
    await _supersede(run, LIQUIDATIONS, identity)
    if status != _LIQUIDATED or debt == 0 or sold == 0:
        await _unposted(run, key)
        return
    if coin == collateral:
        run.flag(f"{LIQUIDATIONS} names one asset twice")
        await _unposted(run, key)
        return
    repaid, given = await run.asset(coin), await run.asset(collateral)
    if repaid is None or given is None:
        await _unposted(run, key)
        return
    postings = [
        PostingInput(await run.account("earn"), given, -sold),
        PostingInput(
            await run.account(LOANS),
            repaid,
            debt,
            cost_per_unit=unit_cost(sold, debt),
            cost_instrument_id=given,
        ),
    ]
    description = f"Crypto loan liquidation {collateral} for {coin}"
    transaction_id = await post(
        run, key, occurred_at=at, description=description, postings=postings
    )
    await run.typed(transaction_id=transaction_id)


# A flexible loan's interest, derived from the reports.


def _not_interest(connection_id: UUID) -> ColumnElement[bool]:
    t = models.Transaction
    return not_(
        and_(
            t.connection_id == connection_id,
            func.coalesce(t.external_id, "").startswith(INTEREST_PREFIX),
        )
    )


async def _reported(run: Run, account_id: UUID) -> dict[tuple[UUID, datetime], Decimal]:
    """Each (instrument, report time) the account's reported snapshots give, the latest recorded
    of a time (as `reconcile.reported_at`)."""
    s = models.BalanceSnapshot
    rows = await run.session.execute(
        select(s.instrument_id, s.as_of, s.quantity)
        .where(s.account_id == account_id, s.kind == "reported")
        .order_by(s.instrument_id, s.as_of, s.recorded_at, s.id)
    )
    return {(row.instrument_id, row.as_of): row.quantity for row in rows}


async def _posted(run: Run, account_id: UUID) -> dict[UUID, tuple[list[datetime], list[Decimal]]]:
    """Per instrument, the times of the account's non-void postings other than the derived
    interest, oldest first, and the running sum of their quantities."""
    t, p = models.Transaction, models.Posting
    rows = await run.session.execute(
        select(p.instrument_id, t.occurred_at, p.quantity)
        .join(t, t.id == p.transaction_id)
        .where(p.account_id == account_id, t.status != "void", _not_interest(run.connection.id))
        .order_by(p.instrument_id, t.occurred_at)
    )
    found: dict[UUID, tuple[list[datetime], list[Decimal]]] = {}
    for row in rows:
        times, sums = found.setdefault(row.instrument_id, ([], []))
        times.append(row.occurred_at)
        sums.append((sums[-1] if sums else Decimal(0)) + row.quantity)
    return found


def _sum_between(
    posted: tuple[list[datetime], list[Decimal]], after: datetime, through: datetime
) -> Decimal:
    times, sums = posted
    high, low = bisect.bisect_right(times, through), bisect.bisect_right(times, after)
    upper = sums[high - 1] if high else Decimal(0)
    lower = sums[low - 1] if low else Decimal(0)
    return upper - lower


class _FlexibleCoins:
    """Which coins had a flexible loan when, from one read of each source: the coins of each
    flexible report (by its read's start; an answer listing none lists no coin) and the times of
    each coin's flexible borrows, repays and liquidations."""

    def __init__(
        self, reports: list[tuple[datetime, frozenset[str]]], moved: dict[str, list[datetime]]
    ):
        self._times = [at for at, _ in reports]
        self._coins = [coins for _, coins in reports]
        self._moved = moved

    @classmethod
    async def load(cls, run: Run) -> "_FlexibleCoins":
        ongoing = layout.table_name(FLEXIBLE_ORDERS)
        c = {"c": run.connection.id}
        answers = await run.session.execute(
            sql(
                "select r.snapshot_at,"
                " array_remove(array_agg(distinct o.loan_coin), null) as coins"
                f" from finance.{ongoing}_responses r left join finance.{ongoing} o"
                "  on o.connection_id = r.connection_id and o.snapshot_at = r.snapshot_at"
                " where r.connection_id = :c group by r.snapshot_at order by r.snapshot_at"
            ),
            c,
        )
        reports: list[tuple[datetime, frozenset[str]]] = [
            (row.snapshot_at, frozenset[str](row.coins or ())) for row in answers
        ]
        parts = [
            f"select loan_coin, {column} as at from finance.{layout.table_name(label)}"
            " where connection_id = :c"
            for label, column in _FLEXIBLE_HISTORIES.items()
        ]
        moved: dict[str, list[datetime]] = {}
        rows = await run.session.execute(sql(" union all ".join(parts) + " order by at"), c)
        for row in rows:
            moved.setdefault(row.loan_coin, []).append(row.at)
        return cls(reports, moved)

    def _listed(self, coin: str, at: datetime) -> bool:
        index = bisect.bisect_right(self._times, at) - 1
        return index >= 0 and coin in self._coins[index]

    def had(self, coin: str, after: datetime, through: datetime) -> bool:
        """Whether the coin had a flexible loan in (after, through]: an ongoing flexible order
        of it in the latest flexible report up to either end, or a flexible borrow, repay or
        liquidation of it inside the interval."""
        if self._listed(coin, after) or self._listed(coin, through):
            return True
        times = self._moved.get(coin, [])
        return bisect.bisect_right(times, through) > bisect.bisect_right(times, after)


type _Shape = tuple[datetime, tuple[tuple[UUID, UUID, Decimal, bool, str], ...]]


async def _held_interest(run: Run) -> dict[str, _Shape]:
    """The connection's live `loan_interest:` transactions: each one's time and postings
    (account, instrument, quantity, fee, memo) in order, from one query."""
    t, p = models.Transaction, models.Posting
    rows = await run.session.execute(
        select(
            t.external_id,
            t.occurred_at,
            p.account_id,
            p.instrument_id,
            p.quantity,
            p.is_fee,
            p.memo,
        )
        .join(p, p.transaction_id == t.id)
        .where(
            t.connection_id == run.connection.id,
            t.status != "void",
            t.external_id.startswith(INTEREST_PREFIX),
        )
        .order_by(t.external_id, p.position)
    )
    held: dict[str, tuple[datetime, list[tuple[UUID, UUID, Decimal, bool, str]]]] = {}
    for row in rows:
        if row.external_id is None:
            continue
        _, postings = held.setdefault(row.external_id, (row.occurred_at, []))
        postings.append((row.account_id, row.instrument_id, row.quantity, row.is_fee, row.memo))
    return {key: (at, tuple(postings)) for key, (at, postings) in held.items()}


def _shape(at: datetime, postings: list[PostingInput]) -> _Shape:
    return at, tuple(
        (p.account_id, p.instrument_id, p.quantity, p.is_fee, p.memo) for p in postings
    )


async def set_interest(run: Run) -> None:
    """A flexible loan's interest, derived afresh on every run as the opening is (design 5.7):
    for each interval between two consecutive reports of the Crypto Loans account, and each coin
    that had a flexible loan in it, the growth of the debt that the account's postings (other
    than these) do not explain, when it is beyond the coin's tolerance, is
    `loan_interest:<coin>:<later report ms>`: a fee on the loan against Unassigned, dated at the
    later report. A fall nothing explains stays a reconciliation difference; a VIP loan's
    interest is posted when it is paid."""
    wanted: dict[str, tuple[datetime, str, list[PostingInput]]] = {}
    flexible: _FlexibleCoins | None = None
    for account_id, wallet in (await wallets(run.session, run.connection.id)).items():
        if wallet != LOANS:
            continue
        reported = await _reported(run, account_id)
        posted = await _posted(run, account_id)
        instruments = sorted({i for i, _ in reported} | set(posted))
        decimals: dict[UUID, int] = {}
        codes: dict[UUID, str] = {}
        times = await report_times(run.session, account_id)
        for (after, through), instrument in itertools.product(
            itertools.pairwise(times), instruments
        ):
            zero = Decimal(0)
            change = reported.get((instrument, through), zero) - reported.get(
                (instrument, after), zero
            )
            explained = (
                _sum_between(posted[instrument], after, through) if instrument in posted else zero
            )
            grown = explained - change
            if instrument not in decimals:
                decimals[instrument] = (
                    await run.session.get_one(models.Instrument, instrument)
                ).decimals
            if grown <= tolerance(decimals[instrument]):
                continue
            if instrument not in codes:
                codes[instrument] = await asset_code(run.session, instrument)
            coin = codes[instrument]
            if flexible is None:
                flexible = await _FlexibleCoins.load(run)
            if flexible.had(coin, after, through):
                key = f"{INTEREST_PREFIX}{coin}:{tables.millis(through)}"
                postings = fees(account_id, run.unassigned, [(instrument, grown, INTEREST_MEMO)])
                wanted[key] = (through, coin, postings)
    held = await _held_interest(run)
    # Only what is new, changed or no longer explained is written: an unchanged one is left as
    # it is, as `post` would leave it.
    for key in sorted(set(held) - set(wanted)):
        await remove(run, key)
    for key in sorted(wanted):
        through, coin, postings = wanted[key]
        if held.get(key) == _shape(through, postings):
            continue
        await post(
            run,
            key,
            occurred_at=through,
            description=f"Crypto loan interest {coin}",
            postings=postings,
        )
