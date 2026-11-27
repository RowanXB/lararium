"""Crypto loans in the ledger (design 5.4, 5.6, 5.7): the debt is a negative holding of the loan
coin on the Crypto Loans account, reported as minus the ongoing orders' `totalDebt`; a borrow
moves the coin to Spot against that debt, a repay back; a VIP repay's interest is a fee on the
loan, and a flexible loan's interest, which Binance lists nowhere, is the debt's growth the
records do not explain; a liquidation sells the collateral from Earn; collateral moves are
typed only."""

from datetime import date
from decimal import Decimal

from sqlalchemy import text

from lararium.modules.finance.binance.balances import (
    LOAN_READS,
    Read,
    ambiguous,
    loan_bracket,
    report_balances,
    settle_reads,
)
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import (
    BALANCE_LABELS,
    HISTORY_LABELS,
    PHASE_B_BUILT,
    to_json,
    walk,
)
from lararium.modules.finance.binance.reconcile import check
from lararium.modules.finance.binance.records import unit_cost
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.transfers import find_transfer_violations
from lararium.modules.finance.valuation import holdings_asof

from .fixtures import BASE_TIME_MS, HOUR_MS, NOW_MS, FakeBinance, build_response, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    keys,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    transaction,
    typed,
)
from .syncing import (
    Clock,
    RecordingNotifier,
    binance_connection,
    client_for,
    context,
    later_config,
    run_binance_sync,
)

T = BASE_TIME_MS
LOANS = "Binance Crypto Loans"
BORROWS = "loan-flexible-borrows"
REPAYS = "loan-flexible-repays"
LTV = "loan-flexible-ltv-adjustments"
LIQUIDATIONS = "loan-flexible-liquidations"
FLEXIBLE = "loan-flexible-ongoing"
VIP = "vip-loan-ongoing"
VIP_REPAYS = "vip-loan-repays"
VIP_INTEREST = "vip-loan-interest"
VIP_REQUESTS = "vip-loan-requests"
VIP_COLLATERAL = "vip-loan-collateral-account"
_CHANGED = "changed by something no record explains"

HISTORIES = (BORROWS, REPAYS, LTV, LIQUIDATIONS, VIP_REPAYS, VIP_INTEREST, VIP_REQUESTS)
REPORTS = (FLEXIBLE, VIP, VIP_COLLATERAL)


def _borrow(seq: int = 0, **fields) -> dict:
    values = {
        "loanCoin": "AAA",
        "initialLoanAmount": "100",
        "collateralCoin": "STB",
        "initialCollateralAmount": "50",
        "borrowTime": T + seq * HOUR_MS,
        "status": "SUCCESS",
    }
    return build_row(BORROWS, seq, **(values | fields))


def _repay(seq: int = 0, **fields) -> dict:
    values = {
        "loanCoin": "AAA",
        "repayAmount": "30",
        "collateralCoin": "STB",
        "collateralReturn": "0",
        "repayStatus": "REPAID",
        "repayTime": T + seq * HOUR_MS,
    }
    return build_row(REPAYS, seq, **(values | fields))


def _vip_repay(seq: int = 0, **fields) -> dict:
    values = {
        "loanCoin": "BBB",
        "repayAmount": "10.5",
        "repayPrincipal": "10",
        "repayInterest": "0.5",
        "collateralCoin": "STB",
        "repayStatus": "Repaid",
        "loanDate": str(T - 30 * 24 * HOUR_MS),
        "repayTime": str(T + seq * HOUR_MS),
        "orderId": "900",
    }
    return build_row(VIP_REPAYS, seq, **(values | fields))


def _flexible(coin: str, debt: str, collateral: str = "STB") -> dict:
    return build_row(
        FLEXIBLE, loanCoin=coin, totalDebt=debt, collateralCoin=collateral, collateralAmount="1"
    )


def _vip(coin: str, debt: str, order: int = 900) -> dict:
    return build_row(VIP, orderId=order, loanCoin=coin, totalDebt=debt, collateralCoin="STB")


def _read(label: str, rows: list, at_ms: int, span_ms: int = 200) -> Read:
    return Read(label, to_json(build_response(label, rows)), at(at_ms), at(at_ms + span_ms))


def _loans(at_ms: int, flexible: list, vip: list | None = None) -> dict[str, Read | None]:
    """The two reads of the Crypto Loans wallet, one after the other (VIP left out: not
    present)."""
    reads: dict[str, Read | None] = {FLEXIBLE: _read(FLEXIBLE, flexible, at_ms)}
    if vip is not None:
        reads[VIP] = _read(VIP, vip, at_ms + 500)
    return reads


async def _snapshots(session, connection_id) -> list[tuple]:
    rows = await session.execute(
        text(
            "select a.name, i.symbol, s.quantity, s.as_of from finance.balance_snapshots s"
            " join finance.accounts a on a.id = s.account_id"
            " join finance.instruments i on i.id = s.instrument_id"
            " where a.source_connection_id = :c and s.kind = 'reported'"
            " order by s.as_of, a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


async def _instrument(session, symbol: str):
    return await session.scalar(
        text("select id from finance.instruments where symbol = :s"), {"s": symbol}
    )


async def _net_worth(session, connection_id, day: date) -> Decimal:
    """The value of the connection's holdings on the day, every one of them priced."""
    accounts = set(
        await session.scalars(
            text("select id from finance.accounts where source_connection_id = :c"),
            {"c": connection_id},
        )
    )
    rows = [h for h in await holdings_asof(session, day) if h.account_id in accounts]
    assert all(h.status == "ok" for h in rows), rows
    return sum((h.value for h in rows if h.value is not None), Decimal(0))


async def _price(run, session, symbol: str, price: str, day: date) -> None:
    gbp = await session.scalar(
        text(
            "select instrument_id from finance.instrument_identifiers"
            " where scheme = 'iso4217' and value = 'GBP'"
        )
    )
    await run(
        record_price,
        instrument_id=await _instrument(session, symbol),
        quote_instrument_id=gbp,
        price=Decimal(price),
        as_of_date=day,
    )


async def _never_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


def _borrow_key(row: dict, occurrence: int = 1) -> str:
    return (
        f"loan_flexible_borrow:{row['loanCoin']}:{row['collateralCoin']}:{row['borrowTime']}"
        f":{row['initialLoanAmount']}:{occurrence}"
    )


def _repay_key(row: dict, occurrence: int = 1) -> str:
    return (
        f"loan_flexible_repay:{row['loanCoin']}:{row['collateralCoin']}:{row['repayTime']}"
        f":{row['repayAmount']}:{occurrence}"
    )


def test_the_family_is_walked_and_read() -> None:
    assert set(HISTORIES) | set(REPORTS) <= set(PHASE_B_BUILT)
    assert set(HISTORIES) <= set(HISTORY_LABELS)
    assert set(REPORTS) <= set(BALANCE_LABELS)
    assert all(ENDPOINTS[label].kind == "position" for label in REPORTS)
    # The two reads of the Crypto Loans wallet are made one after the other.
    assert LOAN_READS == (FLEXIBLE, VIP)
    first = BALANCE_LABELS.index(FLEXIBLE)
    assert BALANCE_LABELS[first + 1] == VIP


# Borrowing and repaying.


async def test_borrow_does_not_change_net_worth(session, run) -> None:
    connection = await binance_connection(run)
    borrowed = _borrow(0)
    pending = _borrow(1, status="PENDING", initialLoanAmount="7")
    failed = _borrow(2, status="FAILED", initialLoanAmount="8")

    await normalize_rows(session, connection.id, (BORROWS, [borrowed, pending, failed]))

    key = _borrow_key(borrowed)
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(100)),
        (LOANS, "AAA", Decimal(-100)),
    ]
    assert await _never_transfer(session, key) is True
    assert (await transaction(session, key)).occurred_at == at(T)
    assert await keys(session, connection.id) == [key]
    # The coin borrowed and the debt cancel: what the account is worth does not move.
    assert await holdings(session, connection.id) == {
        ("Binance Spot", "AAA"): Decimal(100),
        (LOANS, "AAA"): Decimal(-100),
    }
    account_type = await session.execute(
        text("select ledger_type, subtype, balance_mode from finance.accounts where name = :n"),
        {"n": LOANS},
    )
    assert tuple(account_type.one()) == ("liability", "other", "hybrid")
    day = at(T).date()
    await _price(run, session, "AAA", "3", day)
    assert await _net_worth(session, connection.id, day) == 0
    assert await find_transfer_violations(session) == []
    await assert_balanced_and_quiet(session, connection.id)


async def test_borrow_seen_pending_then_done_is_one_movement(session, run) -> None:
    connection = await binance_connection(run)
    pending = _borrow(0, status="PENDING")
    await normalize_rows(session, connection.id, (BORROWS, [pending]))
    assert await keys(session, connection.id) == []

    done = {**pending, "status": "SUCCESS"}
    await normalize_rows(session, connection.id, (BORROWS, [done]))

    assert await keys(session, connection.id) == [_borrow_key(done)]
    rows = await typed(session, "binance_loan_flexible_borrows", connection.id)
    assert [(r.status, r.transaction_id is not None) for r in rows] == [("SUCCESS", True)]
    await assert_balanced_and_quiet(session, connection.id)


async def test_repay_returns_the_coin_to_the_loan(session, run) -> None:
    connection = await binance_connection(run)
    repaying = _repay(0, repayStatus="REPAYING")
    failed = _repay(1, repayStatus="FAILED", repayAmount="4")
    await normalize_rows(session, connection.id, (REPAYS, [repaying, failed]))
    assert await keys(session, connection.id) == []

    repaid = {**repaying, "repayStatus": "REPAID"}
    await normalize_rows(session, connection.id, (REPAYS, [repaid]))

    key = _repay_key(repaid)
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-30)),
        (LOANS, "AAA", Decimal(30)),
    ]
    assert await _never_transfer(session, key) is True
    assert await keys(session, connection.id) == [key]
    rows = await typed(session, "binance_loan_flexible_repays", connection.id)
    assert sorted((r.repay_status, r.transaction_id is not None) for r in rows) == [
        ("FAILED", False),
        ("REPAID", True),
    ]
    await assert_balanced_and_quiet(session, connection.id)


async def test_liquidation_sells_the_collateral_from_earn(session, run) -> None:
    connection = await binance_connection(run)
    liquidated = build_row(
        LIQUIDATIONS,
        loanCoin="AAA",
        liquidationDebt="40",
        collateralCoin="STB",
        liquidationCollateralAmount="45",
        returnCollateralAmount="2",
        liquidationFee="0.9",
        liquidationStartingPrice="1.1",
        liquidationStartingTime=T,
        status="Liquidated",
    )
    liquidating = build_row(
        LIQUIDATIONS, 1, loanCoin="BBB", collateralCoin="STB", status="Liquidating"
    )

    await normalize_rows(session, connection.id, (LIQUIDATIONS, [liquidated, liquidating]))

    key = f"loan_flexible_liquidation:AAA:STB:{T}:40:1"
    found = await postings(session, key)
    assert [r[:5] for r in found] == [
        ("Binance Earn", "STB", Decimal(-45), None, None),
        (LOANS, "AAA", Decimal(40), unit_cost(Decimal(45), Decimal(40)), "STB"),
    ]
    assert await _never_transfer(session, key) is True
    assert await keys(session, connection.id) == [key]
    await assert_balanced_and_quiet(session, connection.id)


async def test_collateral_is_not_counted_twice(session, run) -> None:
    # The collateral of a flexible loan is a Simple Earn flexible position: the subscription
    # Binance lists for it (type LOAN) moves it from Spot to Earn, and nothing else does.
    connection = await binance_connection(run)
    borrowed = _borrow(0)
    pledged = build_row(
        "earn-flexible-subscriptions",
        amount="50",
        asset="STB",
        time=T,
        type="LOAN",
        sourceAccount="SPOT",
        status="SUCCESS",
    )
    added = build_row(
        LTV, loanCoin="AAA", collateralCoin="STB", direction="ADDITIONAL", collateralAmount="5"
    )
    pledged_more = build_row(
        "earn-flexible-subscriptions",
        1,
        amount="5",
        asset="STB",
        time=T + HOUR_MS,
        type="LOAN",
        sourceAccount="SPOT",
        status="SUCCESS",
    )
    record = build_row("earn-flexible-collateral", asset="STB", amount="5", type="REPAY")

    await normalize_rows(
        session,
        connection.id,
        (BORROWS, [borrowed]),
        ("earn-flexible-subscriptions", [pledged, pledged_more]),
        (LTV, [added]),
        ("earn-flexible-collateral", [record]),
    )

    assert await holdings(session, connection.id) == {
        ("Binance Spot", "AAA"): Decimal(100),
        (LOANS, "AAA"): Decimal(-100),
        ("Binance Spot", "STB"): Decimal(-55),
        ("Binance Earn", "STB"): Decimal(55),
    }
    [adjustment] = await typed(session, "binance_loan_flexible_ltv_adjustments", connection.id)
    assert adjustment.transaction_id is None
    assert len(await keys(session, connection.id)) == 3
    await assert_balanced_and_quiet(session, connection.id)


async def test_vip_records_without_a_movement_are_typed_only(session, run) -> None:
    connection = await binance_connection(run)
    interest = build_row(VIP_INTEREST, loanCoin="BBB", interestAmount="0.1", orderId=900)
    request = build_row(VIP_REQUESTS, loanCoin="BBB", loanAmount="50", status="Accruing_Interest")

    await normalize_rows(
        session, connection.id, (VIP_INTEREST, [interest]), (VIP_REQUESTS, [request])
    )

    assert await keys(session, connection.id) == []
    [accrued] = await typed(session, "binance_vip_loan_interest", connection.id)
    assert accrued.interest_amount == Decimal("0.1")
    [asked] = await typed(session, "binance_vip_loan_requests", connection.id)
    assert (asked.loan_amount, asked.transaction_id) == (Decimal(50), None)
    await assert_balanced_and_quiet(session, connection.id)


# The debt as reported.


async def test_debt_is_a_negative_holding(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    flexible = [_flexible("AAA", "60"), _flexible("AAA", "10", "CCC"), _flexible("BBB", "5")]
    reads = _loans(NOW_MS, flexible, [_vip("AAA", "30")])
    listed = build_row("wallet-balance", walletName="Spot", balance="0", assetBalances=[])
    reads["wallet-balance"] = _read("wallet-balance", [listed], NOW_MS + 1_000)

    await run(report_balances, connection, reads, flag=flags.append)

    # One cutoff for both reads: the first one's start.
    assert await _snapshots(session, connection.id) == [
        (LOANS, "AAA", Decimal(-100), at(NOW_MS)),
        (LOANS, "BBB", Decimal(-5), at(NOW_MS)),
    ]
    # The wallet list does not name Crypto Loans: nothing to flag.
    assert flags == []
    day = at(NOW_MS).date()
    await _price(run, session, "AAA", "2", day)
    await _price(run, session, "BBB", "4", day)
    assert await _net_worth(session, connection.id, day) == Decimal(-220)

    # The BBB loan is repaid in full: the report no longer lists it, and its debt reads 0.
    later = NOW_MS + HOUR_MS
    await run(report_balances, connection, _loans(later, [_flexible("AAA", "60")]))
    assert (await _snapshots(session, connection.id))[2:] == [
        (LOANS, "AAA", Decimal(-60), at(later)),
        (LOANS, "BBB", Decimal(0), at(later)),
    ]


async def test_an_unclear_loan_read_writes_no_debt(session, run) -> None:
    connection = await binance_connection(run)
    reads = _loans(NOW_MS, [_flexible("AAA", "60")], [_vip("BBB", "5")])
    vip = reads[VIP]
    assert vip is not None
    reads[VIP] = Read(vip.label, vip.body, vip.read_from, vip.read_to, unclear=True)

    await run(report_balances, connection, reads)

    assert await _snapshots(session, connection.id) == []


async def test_a_loan_answer_without_its_list_is_empty_only_when_it_says_so(session, run) -> None:
    connection = await binance_connection(run)
    empty = Read(FLEXIBLE, {"total": 0}, at(NOW_MS), at(NOW_MS + 100))
    await run(
        report_balances,
        connection,
        {FLEXIBLE: _read(FLEXIBLE, [_flexible("AAA", "1")], NOW_MS - HOUR_MS)},
    )

    await run(report_balances, connection, {FLEXIBLE: empty})

    assert (await _snapshots(session, connection.id))[-1] == (
        LOANS,
        "AAA",
        Decimal(0),
        at(NOW_MS),
    )


async def test_a_record_between_the_two_loan_reads_has_both_read_again(
    session, run, fake: FakeBinance, clock: Clock
) -> None:
    # The two reads are one report, cut at the first one's start: a repay between them, near
    # neither, may be in the second read and not the first, so both are made again.
    connection = await binance_connection(run, config=later_config())
    async with fake.http() as http:
        ctx = await context(session, connection.id, http)
        client = client_for(clock, http)
        await client.sync_clock()
        start = clock.ms
        reads = {
            FLEXIBLE: _read(FLEXIBLE, [_flexible("AAA", "1")], start + 1_000),
            VIP: _read(VIP, [_vip("BBB", "40")], start + 10_000),
        }
        fake.add(VIP_REPAYS, _vip_repay(0, repayTime=str(start + 5_000)))
        clock.ms = start + 20_000
        await walk(ctx, client, VIP_REPAYS)

        settled = await settle_reads(ctx, client, reads)

    assert len(fake.calls(FLEXIBLE)) == len(fake.calls(VIP)) == 1
    for label in LOAN_READS:
        again = settled[label]
        assert again is not None and again.read_from >= at(start + 20_000) and not again.unclear
    bracket = loan_bracket(settled)
    assert bracket is not None and not ambiguous(bracket, [at(start + 5_000)])


# Interest.


async def test_interest_is_a_fee_on_the_loan(session, run) -> None:
    connection = await binance_connection(run)
    # A VIP repay names its interest: that part is a fee on the loan.
    vip_repaid = _vip_repay(0)
    await normalize_rows(session, connection.id, (VIP_REPAYS, [vip_repaid]))
    vip_key = f"vip_loan_repay:900:{T}:1"
    assert await postings(session, vip_key) == [
        ("Binance Spot", "BBB", Decimal("-10.5"), None, None, None, None, False, ""),
        (LOANS, "BBB", Decimal("10.5"), None, None, None, None, False, ""),
        (LOANS, "BBB", Decimal("-0.5"), None, None, None, None, True, "loan interest"),
        ("Unassigned", "BBB", Decimal("0.5"), None, None, None, None, False, ""),
    ]
    assert await _never_transfer(session, vip_key) is True

    # A flexible loan's interest is in its debt and nowhere else: what the debt grew by
    # between two reports beyond its records is a fee on the loan at the later report.
    start = T + HOUR_MS
    await run(report_balances, connection, _loans(start, [_flexible("AAA", "100")]))
    await ingest(session, connection.id, REPAYS, [_repay(2, repayAmount="30")])
    end = start + 4 * HOUR_MS
    await run(report_balances, connection, _loans(end, [_flexible("AAA", "70.4")]))
    await run_normalize(session, connection.id)

    interest_key = f"loan_interest:AAA:{end}"
    assert await postings(session, interest_key) == [
        (LOANS, "AAA", Decimal("-0.4"), None, None, None, None, True, "loan interest"),
        ("Unassigned", "AAA", Decimal("0.4"), None, None, None, None, False, ""),
    ]
    assert (await transaction(session, interest_key)).occurred_at == at(end)
    assert await _never_transfer(session, interest_key) is True
    # It explains the change: no difference on the loan.
    assert [d for d in await check(session, connection.id, ()) if d.to_at == at(end)] == []
    await assert_balanced_and_quiet(session, connection.id)

    # It is derived afresh: a record that arrives later and explains the change takes its place.
    await ingest(session, connection.id, BORROWS, [_borrow(3, initialLoanAmount="0.4")])
    await run_normalize(session, connection.id)
    assert await postings(session, interest_key) == []
    await assert_balanced_and_quiet(session, connection.id)


async def test_vip_debt_growth_is_no_interest(session, run) -> None:
    # VIP interest is posted when it is paid (its repay names it): a VIP debt that grows with no
    # record is a difference, never interest.
    connection = await binance_connection(run)
    start = T
    await run(report_balances, connection, _loans(start, [], [_vip("BBB", "40")]))
    end = start + HOUR_MS
    await run(report_balances, connection, _loans(end, [], [_vip("BBB", "41")]))
    await run_normalize(session, connection.id)

    assert await keys(session, connection.id) == []
    [difference] = await check(session, connection.id, ())
    assert (difference.from_at, difference.to_at) == (at(start), at(end))


async def test_a_debt_that_falls_with_no_record_is_no_interest(session, run) -> None:
    connection = await binance_connection(run)
    start = T
    await run(report_balances, connection, _loans(start, [_flexible("AAA", "100")]))
    end = start + HOUR_MS
    await run(report_balances, connection, _loans(end, [_flexible("AAA", "70")]))
    await run_normalize(session, connection.id)

    assert await keys(session, connection.id) == []
    [difference] = await check(session, connection.id, ())
    assert difference.reported_change == Decimal(30)


# Reconciliation: the Crypto Loans account with a consistent history.


def _spot(*held: tuple[str, str]) -> list[dict]:
    return [build_row("spot-account", asset=coin, free=q, locked="0") for coin, q in held]


async def _differences(session, connection_id) -> list:
    rows = await session.execute(
        text(
            "select a.name, i.symbol from finance.binance_reconciliation_differences d"
            " join finance.accounts a on a.id = d.account_id"
            " join finance.instruments i on i.id = d.instrument_id"
            " where a.source_connection_id = :c and d.closed_at is null"
            " order by a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


async def _two_syncs(
    session, run, fake: FakeBinance, clock: Clock, *, listed: bool = True
) -> tuple[object, RecordingNotifier]:
    """A first sync with a VIP loan and no history; then a flexible loan borrowed and partly
    repaid, and part of the VIP loan repaid, before a second sync. With `listed` False the
    flexible repay is not listed."""
    fake.set_rows("spot-account", _spot(("AAA", "20"), ("BBB", "20")))
    fake.set_rows(VIP, [_vip("BBB", "50")])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    start = clock.ms

    def hours(n: int) -> int:
        return start + n * HOUR_MS

    fake.add(BORROWS, _borrow(0, borrowTime=hours(1)))
    if listed:
        fake.add(REPAYS, _repay(0, repayAmount="30.2", repayTime=hours(3)))
    fake.add(VIP_REPAYS, _vip_repay(0, repayTime=str(hours(2))))
    fake.set_rows("spot-account", _spot(("AAA", "89.8"), ("BBB", "9.5")))
    fake.set_rows(FLEXIBLE, [_flexible("AAA", "70.5")])
    fake.set_rows(VIP, [_vip("BBB", "40")])
    notifier = RecordingNotifier()
    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    assert after.status == "healthy", after.last_error
    return after, notifier


async def test_loans_reconcile_with_a_consistent_history(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    after, notifier = await _two_syncs(session, run, fake, clock)

    assert await _differences(session, after.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    live = await keys(session, after.id)
    # The VIP loan had no borrow record: its debt before the first report is the opening.
    bbb = await _instrument(session, "BBB")
    opening = f"opening:loans:{bbb}"
    assert opening in live
    assert plain(await postings(session, opening)) == [
        (LOANS, "BBB", Decimal(-50)),
        ("Opening balances", "BBB", Decimal(50)),
    ]
    # The flexible loan's 0.7 of interest is the debt's growth its records do not explain.
    [interest] = [k for k in live if k.startswith("loan_interest:AAA:")]
    assert plain(await postings(session, interest)) == [
        (LOANS, "AAA", Decimal("-0.7")),
        ("Unassigned", "AAA", Decimal("0.7")),
    ]
    assert await find_transfer_violations(session) == []


async def test_a_missing_repay_is_a_difference_on_the_loan(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    after, _ = await _two_syncs(session, run, fake, clock, listed=False)

    assert await _differences(session, after.id) == [(LOANS, "AAA"), ("Binance Spot", "AAA")]
    assert not [k for k in await keys(session, after.id) if k.startswith("loan_interest:")]
