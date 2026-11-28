from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, text

from lararium.modules.finance import models
from lararium.modules.finance.accounts import set_excluded_from_reports
from lararium.modules.finance.cashflow import (
    CategoryTotal,
    HiddenAccountTotal,
    PayeeTotal,
    cash_flow,
    flows,
)
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.instruments import create_instrument
from lararium.modules.finance.ledger import (
    CategorySplit,
    CounterpartyFields,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.owner_categories import set_owner_category
from lararium.modules.finance.pricing import record_fx_rate, record_price
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.valuation import holdings_asof, net_worth_series

LON = ZoneInfo("Europe/London")
START, END = date(2026, 10, 1), date(2026, 11, 30)


@pytest.fixture
async def bank(make_account):
    return await make_account(name="Bank", balance_mode="transactions")


@pytest.fixture
async def unassigned(run):
    return await run(get_or_create_system_account, "unassigned")


@pytest.fixture
def move(run, bank, unassigned, gbp):
    """Post `amount` to an account (the bank by default) against `contra` (Unassigned)."""

    async def _move(amount: str, day: date, *, account=None, contra=None, instrument=None, **more):
        details = {
            key: more.pop(key)
            for key in (
                "category",
                "include_in_spending",
                "category_splits",
                "income_kind",
                "refund",
            )
            if key in more
        }
        instrument_id = (instrument or gbp).id
        return await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(day.year, day.month, day.day, 12, tzinfo=LON),
                description=more.pop("description", ""),
                postings=[
                    PostingInput((account or bank).id, instrument_id, Decimal(amount)),
                    PostingInput(contra or unassigned, instrument_id, -Decimal(amount)),
                ],
                counterparty=CounterpartyFields(name=more.pop("payee", None)),
                details=TransactionDetails(**details),
            ),
        )

    return _move


async def test_income_and_spending_by_period_category_and_payee(session, move) -> None:
    await move("2000", date(2026, 10, 28), category="income", payee="Employer")
    await move("-30", date(2026, 10, 30), category="groceries", payee="Shop")
    await move("-12.50", date(2026, 11, 2), category="groceries", payee="Shop")
    await move("-7", date(2026, 11, 3), description="Coffee")

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending, flow.missing_count) == (
        Decimal("2000"),
        Decimal("49.50"),
        0,
    )
    assert [(p.start, p.income, p.spending) for p in flow.periods] == [
        (date(2026, 10, 1), Decimal("2000"), Decimal("30")),
        (date(2026, 11, 1), Decimal("0"), Decimal("19.50")),
    ]
    assert flow.categories == [
        CategoryTotal("groceries", Decimal("0"), Decimal("42.50")),
        CategoryTotal(None, Decimal("0"), Decimal("7")),
        CategoryTotal("income", Decimal("2000"), Decimal("0")),
    ]
    assert flow.payees == [PayeeTotal("Shop", Decimal("42.50")), PayeeTotal("Coffee", Decimal("7"))]


async def test_every_period_of_the_range_is_listed(session, move) -> None:
    await move("-5", date(2026, 11, 4))
    days = await cash_flow(session, date(2026, 11, 3), date(2026, 11, 5), "day")
    assert [(p.start, p.spending) for p in days.periods] == [
        (date(2026, 11, 3), Decimal("0")),
        (date(2026, 11, 4), Decimal("5")),
        (date(2026, 11, 5), Decimal("0")),
    ]
    # Weeks start on Monday; 2026-11-04 is a Wednesday.
    weeks = await cash_flow(session, date(2026, 11, 1), date(2026, 11, 10), "week")
    assert [p.start for p in weeks.periods] == [
        date(2026, 10, 26),
        date(2026, 11, 2),
        date(2026, 11, 9),
    ]
    assert weeks.periods[1].spending == Decimal("5")


async def test_transfers_opening_balances_and_voids_are_not_flows(
    session, run, move, make_account
) -> None:
    in_transit = await run(get_or_create_system_account, "in_transit")
    opening = await run(get_or_create_system_account, "opening_balances")
    await move("-100", date(2026, 11, 2), contra=in_transit)
    await move("500", date(2026, 11, 1), contra=opening)
    voided = await move("-9", date(2026, 11, 3))
    await run(void_transaction, voided.id)

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending) == (Decimal("0"), Decimal("0"))


async def test_a_cancelled_pair_is_neither_income_nor_spending(
    session, run, move, bank, gbp
) -> None:
    in_transit = await run(get_or_create_system_account, "in_transit")
    request = await move("-100", date(2026, 11, 2), contra=in_transit, payee="eToro")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 2, 12, 3, tzinfo=LON),
            description="Withdrawal cancelled",
            postings=[
                PostingInput(bank.id, gbp.id, Decimal("100")),
                PostingInput(in_transit, gbp.id, Decimal("-100")),
            ],
            counterparty=CounterpartyFields(name="eToro"),
            cancels_transaction_id=request.id,
        ),
    )

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending, flow.payees) == (Decimal("0"), Decimal("0"), [])


async def test_spending_the_source_says_is_not_spending_is_left_out(session, move) -> None:
    await move("-40", date(2026, 11, 2), include_in_spending=False)
    await move("60", date(2026, 11, 2), include_in_spending=False)

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending) == (Decimal("60"), Decimal("0"))


async def test_category_splits_share_the_amount(session, move) -> None:
    splits = (
        CategorySplit("groceries", Decimal("-20")),
        CategorySplit("eating_out", Decimal("-10")),
    )
    await move("-30", date(2026, 11, 2), category="groceries", category_splits=splits)

    flow = await cash_flow(session, START, END)

    assert {c.category: c.spending for c in flow.categories} == {
        "groceries": Decimal("20"),
        "eating_out": Decimal("10"),
    }
    assert flow.spending == Decimal("30")


async def test_a_refund_lowers_spending_and_is_no_income(session, move) -> None:
    await move("-100", date(2026, 11, 2), category="shopping", payee="Shop")
    await move("60", date(2026, 11, 9), category="shopping", payee="Shop", refund=True)

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending) == (0, 40)
    assert flow.categories == [CategoryTotal("shopping", Decimal(0), Decimal("40"))]
    assert flow.payees == [PayeeTotal("Shop", Decimal("40"))]


async def test_a_refund_counts_in_its_own_month(session, move) -> None:
    await move("-100", date(2026, 10, 30), category="shopping")
    await move("100", date(2026, 11, 3), category="shopping", refund=True)

    flow = await cash_flow(session, date(2026, 10, 1), date(2026, 11, 30))

    assert [(p.start, p.income, p.spending) for p in flow.periods] == [
        (date(2026, 10, 1), 0, 100),
        (date(2026, 11, 1), 0, -100),
    ]


async def test_a_refund_the_source_says_is_not_spending_is_left_out(session, move) -> None:
    await move("60", date(2026, 11, 9), refund=True, include_in_spending=False)

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending) == (0, 0)


async def test_a_refund_shares_its_category_splits(session, move) -> None:
    splits = (CategorySplit("groceries", Decimal("20")), CategorySplit("eating_out", Decimal("10")))
    await move("30", date(2026, 11, 2), category="groceries", category_splits=splits, refund=True)

    flow = await cash_flow(session, START, END)

    assert {c.category: c.spending for c in flow.categories} == {
        "groceries": Decimal("-20"),
        "eating_out": Decimal("-10"),
    }
    assert flow.spending == Decimal("-30")


async def test_a_positive_flow_that_is_no_refund_is_still_income(session, move) -> None:
    await move("60", date(2026, 11, 9))

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.spending) == (60, 0)


async def test_other_currencies_use_the_rate_of_the_day(session, run, move, usd, eur, gbp) -> None:
    await run(
        record_fx_rate,
        base_instrument_id=usd.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.8"),
        as_of_date=date(2026, 11, 1),
    )
    await move("-10", date(2026, 11, 2), instrument=usd)
    await move("-10", date(2026, 11, 2), instrument=eur)  # no rate: left out and counted

    flow = await cash_flow(session, START, END)

    assert (flow.spending, flow.missing_count) == (Decimal("8.0"), 1)


async def test_an_excluded_account_is_left_out_of_every_report(
    session, run, move, make_account, bank
) -> None:
    other = await make_account(name="Other", balance_mode="transactions")
    await move("1000", date(2026, 11, 1))
    await move("300", date(2026, 11, 1), account=other)
    day = date(2026, 11, 2)

    record = await run(set_excluded_from_reports, other.id, True)
    assert record.excluded_from_reports
    excluded = [other.id]

    assert (await cash_flow(session, START, END, excluded=excluded)).income == Decimal("1000")
    assert [h.account_id for h in await holdings_asof(session, day, excluded)] == [bank.id]
    assert [p.value for p in await net_worth_series(session, day, day, excluded)] == [
        Decimal("1000")
    ]
    assert [p.value for p in await net_worth_series(session, day, day)] == [Decimal("1300")]

    assert not (await run(set_excluded_from_reports, other.id, False)).excluded_from_reports


async def test_only_own_asset_and_liability_accounts_can_be_excluded(
    run, make_account, unassigned
) -> None:
    expense = await make_account(name="Costs", ledger_type="expense", subtype="other")
    for account_id in (expense.id, unassigned):
        with pytest.raises(FinanceValidationError):
            await run(set_excluded_from_reports, account_id, True)


async def _transfer(session, *, sent, received, from_account, to_account, gbp, **more) -> None:
    await session.execute(
        insert(models.Transfer).values(
            from_account_id=from_account.id,
            to_account_id=to_account.id,
            instrument_id=gbp.id,
            amount=Decimal("100"),
            status=more.pop("status", "matched"),
            out_transaction_id=sent.id if sent else None,
            in_transaction_id=received.id if received else None,
            opened_at=datetime(2026, 11, 2, 12, tzinfo=LON),
            **more,
        )
    )


async def test_money_moved_to_or_from_a_hidden_account_is_a_flow(
    session, run, move, make_account, bank, gbp
) -> None:
    """A hidden account is as good as someone else's: what moves between it and an account
    still in the reports is income or spending, apart from categories and payees."""
    pension = await make_account(name="Pension", balance_mode="transactions")
    in_transit = await run(get_or_create_system_account, "in_transit")
    out = await move("-100", date(2026, 10, 30), contra=in_transit)
    arrived = await move("100", date(2026, 11, 2), account=pension, contra=in_transit)
    await _transfer(
        session, sent=out, received=arrived, from_account=bank, to_account=pension, gbp=gbp
    )
    back = await move("-30", date(2026, 11, 5), account=pension, contra=in_transit)
    landed = await move("30", date(2026, 11, 5), contra=in_transit)
    await _transfer(
        session, sent=back, received=landed, from_account=pension, to_account=bank, gbp=gbp
    )
    # Not yet arrived: the shown side's record is all there is.
    waiting = await move("-20", date(2026, 11, 20), contra=in_transit)
    await _transfer(
        session,
        sent=waiting,
        received=None,
        from_account=bank,
        to_account=pension,
        gbp=gbp,
        status="awaiting",
    )

    shown = await cash_flow(session, START, END)
    assert (shown.income, shown.spending, shown.hidden_accounts) == (0, 0, [])

    flow = await cash_flow(session, START, END, excluded=[pension.id])
    assert (flow.income, flow.spending) == (Decimal("30"), Decimal("120"))
    assert flow.hidden_accounts == [
        HiddenAccountTotal(pension.id, "Pension", Decimal("30"), Decimal("120"))
    ]
    # Counted on the day the shown side moved, and kept out of categories and payees.
    assert [(p.start, p.income, p.spending) for p in flow.periods] == [
        (date(2026, 10, 1), Decimal("0"), Decimal("100")),
        (date(2026, 11, 1), Decimal("30"), Decimal("20")),
    ]
    assert (flow.categories, flow.payees) == ([], [])


async def test_a_move_straight_to_a_hidden_pot_is_a_flow(
    session, run, make_account, bank, gbp
) -> None:
    """A Monzo pot move posts the main account and the pot in one transaction, with no
    transfer: hiding the pot makes it spending, as net worth falls by it."""
    pot = await make_account(name="Holiday pot", subtype="savings_pot", balance_mode="hybrid")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 2, 12, tzinfo=LON),
            description="Holiday pot",
            postings=[
                PostingInput(bank.id, gbp.id, Decimal("-50")),
                PostingInput(pot.id, gbp.id, Decimal("50")),
            ],
        ),
    )

    assert (await cash_flow(session, START, END)).spending == 0
    flow = await cash_flow(session, START, END, excluded=[pot.id])
    assert (flow.income, flow.spending) == (Decimal("0"), Decimal("50"))
    assert flow.hidden_accounts == [
        HiddenAccountTotal(pot.id, "Holiday pot", Decimal("0"), Decimal("50"))
    ]
    # With the main account hidden instead, the pot's side arrived from it.
    flow = await cash_flow(session, START, END, excluded=[bank.id])
    assert (flow.income, flow.spending) == (Decimal("50"), Decimal("0"))


async def test_a_transfer_fee_goes_with_an_excluded_side(
    session, run, move, make_account, bank, unassigned, gbp
) -> None:
    """A settlement posts only to In transit and Unassigned, so it is left out by the
    transfer it settles: when either of its accounts is excluded."""
    other = await make_account(name="Other", balance_mode="transactions")
    in_transit = await run(get_or_create_system_account, "in_transit")
    day = date(2026, 11, 2)
    sent = await move("-100", day, contra=in_transit)
    received = await move("95", day, account=other, contra=in_transit)
    fee = await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 2, 12, tzinfo=LON),
            description="Transfer fee",
            postings=[
                PostingInput(in_transit, gbp.id, Decimal("-5"), is_fee=True),
                PostingInput(unassigned, gbp.id, Decimal("5")),
            ],
        ),
    )
    await session.execute(
        insert(models.Transfer).values(
            from_account_id=bank.id,
            to_account_id=other.id,
            instrument_id=gbp.id,
            amount=Decimal("100"),
            status="matched",
            out_transaction_id=sent.id,
            in_transaction_id=received.id,
            settlement_transaction_id=fee.id,
            opened_at=datetime(2026, 11, 2, 12, tzinfo=LON),
        )
    )

    assert (await cash_flow(session, START, END)).spending == Decimal("5")
    # With one side hidden, the side still shown moved the whole amount, the fee within it.
    to_hidden = await cash_flow(session, START, END, excluded=[other.id])
    assert (to_hidden.income, to_hidden.spending) == (Decimal("0"), Decimal("100"))
    from_hidden = await cash_flow(session, START, END, excluded=[bank.id])
    assert (from_hidden.income, from_hidden.spending) == (Decimal("95"), Decimal("0"))
    both = await cash_flow(session, START, END, excluded=[bank.id, other.id])
    assert (both.income, both.spending, both.hidden_accounts) == (Decimal("0"), Decimal("0"), [])


async def test_money_on_its_way_to_or_from_a_hidden_account_is_left_out(
    session, run, move, make_account, bank, gbp
) -> None:
    """In transit is shared by every transfer: what it holds for one to or from a hidden
    account goes with that account, so net worth falls the day money leaves for it (when
    cash flow counts the spending) and rises the day money from it arrives."""
    savings = await make_account(name="Savings", balance_mode="transactions")
    in_transit = await run(get_or_create_system_account, "in_transit")
    await move("1000", date(2026, 11, 1))
    out = await move("-100", date(2026, 11, 2), contra=in_transit)
    arrived = await move("100", date(2026, 11, 3), account=savings, contra=in_transit)
    await _transfer(
        session, sent=out, received=arrived, from_account=bank, to_account=savings, gbp=gbp
    )
    day1, day2 = date(2026, 11, 2), date(2026, 11, 3)

    async def worth(excluded):
        return [p.value for p in await net_worth_series(session, day1, day2, excluded)]

    assert await worth([]) == [Decimal("1000"), Decimal("1000")]
    assert await worth([savings.id]) == [Decimal("900"), Decimal("900")]
    assert await worth([bank.id]) == [Decimal("0"), Decimal("100")]
    assert await worth([bank.id, savings.id]) == [Decimal("0"), Decimal("0")]
    # In transit's money is shown until it is a hidden transfer's.
    assert in_transit in {h.account_id for h in await holdings_asof(session, day1)}
    assert in_transit not in {h.account_id for h in await holdings_asof(session, day1, [bank.id])}


async def test_many_transactions_take_one_parameter(session) -> None:
    """Category splits are read with one array parameter: a long range of trades and fees
    can hold more flows than a statement takes parameters (65,535)."""
    from lararium.modules.finance.cashflow import _splits

    assert await _splits(session, [uuid4() for _ in range(70_000)]) == {}


async def test_owner_category_wins_and_covers_splits(session, run, move) -> None:
    """The owner's word is for the whole transaction: it replaces the source's category and
    its splits."""
    splits = (CategorySplit("salary", Decimal("60")), CategorySplit("gift", Decimal("40")))
    tx = await move("100", date(2026, 11, 2), category="transfers", category_splits=splits)
    await run(set_owner_category, [tx.id], "family")

    flow = await cash_flow(session, START, END)

    assert flow.categories == [CategoryTotal("family", Decimal("100"), Decimal("0"))]
    [entry] = await flows(session, START, END, ())
    assert (entry.category, entry.has_owner_category, entry.source_category) == (
        "family",
        True,
        "transfers",
    )
    assert entry.owner_category == "family"


async def _dividend(run, bank, unassigned, gbp):
    """An eToro-like dividend: the gross amount and the tax withheld, both on Unassigned."""
    return await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 2, 12, tzinfo=LON),
            description="Dividend",
            postings=[
                PostingInput(bank.id, gbp.id, Decimal("8.5")),
                PostingInput(unassigned, gbp.id, Decimal("-10")),
                PostingInput(unassigned, gbp.id, Decimal("1.5")),
            ],
            details=TransactionDetails(income_kind="dividend"),
        ),
    )


async def test_income_kind_groups_positive_flows_only(session, run, bank, unassigned, gbp) -> None:
    await _dividend(run, bank, unassigned, gbp)

    flow = await cash_flow(session, START, END)

    assert flow.categories == [
        CategoryTotal(None, Decimal("0"), Decimal("1.5")),
        CategoryTotal("dividend", Decimal("10"), Decimal("0")),
    ]


async def _coin(run, symbol: str, usd, kind: str = "crypto"):
    return await run(
        create_instrument,
        kind=kind,
        symbol=symbol,
        name=symbol,
        decimals=8,
        quote_instrument_id=usd.id,
        identifiers=[],
    )


async def _reward(run, wallet, unassigned, coin, quantity: str, day: date, **details):
    return await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(day.year, day.month, day.day, 12, tzinfo=LON),
            description="Simple Earn reward",
            postings=[
                PostingInput(wallet.id, coin.id, Decimal(quantity)),
                PostingInput(unassigned, coin.id, -Decimal(quantity)),
            ],
            details=TransactionDetails(**details),
        ),
    )


async def test_coin_reward_valued_on_its_day(
    session, run, make_account, unassigned, btc, eur, gbp
) -> None:
    """At the latest price on or before its day, a manual one first among the day's."""
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    day = date(2026, 11, 2)
    for value, on, source in (
        ("40000", date(2026, 11, 1), "manual"),
        ("50000", day, "manual"),
        ("45000", day, "coingecko"),
        ("60000", date(2026, 11, 3), "manual"),
    ):
        await run(
            record_price,
            instrument_id=btc.id,
            quote_instrument_id=eur.id,
            price=Decimal(value),
            as_of_date=on,
            source=source,
        )
    # Recorded later, so only the manual preference keeps it from winning.
    await session.execute(
        text(
            "update finance.prices set recorded_at = now() + interval '1 hour'"
            " where source = 'coingecko'"
        )
    )
    await run(
        record_fx_rate,
        base_instrument_id=eur.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.85"),
        as_of_date=day,
    )
    tx = await _reward(run, wallet, unassigned, btc, "0.0001", day, income_kind="reward")

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.missing_count) == (Decimal("4.25"), 0)
    assert flow.categories == [CategoryTotal("reward", Decimal("4.25"), Decimal("0"))]
    [entry] = await flows(session, START, END, ())
    assert (entry.transaction_id, entry.day, entry.account_id, entry.account_name) == (
        tx.id,
        day,
        wallet.id,
        "Exchange",
    )
    assert (entry.instrument_id, entry.instrument_code, entry.quantity, entry.amount) == (
        btc.id,
        "BTC",
        Decimal("0.0001"),
        Decimal("4.25"),
    )
    assert (entry.category, entry.income_kind, entry.has_owner_category) == (
        "reward",
        "reward",
        False,
    )


async def test_coin_reward_without_price_is_missing(
    session, run, move, make_account, unassigned, btc
) -> None:
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    await move("100", date(2026, 11, 1))
    await _reward(run, wallet, unassigned, btc, "0.0001", date(2026, 11, 2), income_kind="reward")

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.missing_count) == (Decimal("100"), 1)


async def test_coin_reward_without_fx_rate_is_missing(
    session, run, make_account, unassigned, btc, eur
) -> None:
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    day = date(2026, 11, 2)
    await run(
        record_price,
        instrument_id=btc.id,
        quote_instrument_id=eur.id,
        price=Decimal("50000"),
        as_of_date=day,
    )  # but no EUR rate is recorded
    await _reward(run, wallet, unassigned, btc, "0.0001", day, income_kind="reward")

    flow = await cash_flow(session, START, END)

    assert (flow.income, flow.missing_count) == (Decimal("0"), 1)


async def test_coin_without_income_kind_is_not_a_flow(
    session, run, make_account, unassigned, usd
) -> None:
    """A coin spent on a fee, or a security arriving from nowhere, is not money: only a
    source's income kind makes an asset other than money a flow."""
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    bnb = await _coin(run, "BNB", usd)
    stock = await _coin(run, "ACME", usd, kind="equity")
    day = date(2026, 11, 2)
    for coin in (bnb, stock):
        await run(
            record_price,
            instrument_id=coin.id,
            quote_instrument_id=usd.id,
            price=Decimal("10"),
            as_of_date=day,
        )
    await _reward(run, wallet, unassigned, bnb, "-0.01", day)  # a fee paid in BNB
    await _reward(run, wallet, unassigned, stock, "1", day)  # a share arriving

    assert await flows(session, START, END, ()) == []
    flow = await cash_flow(session, START, END)
    assert (flow.income, flow.spending, flow.missing_count) == (0, 0, 0)


async def test_periods_and_categories_add_up_to_the_totals(
    session, run, move, make_account, bank, unassigned, gbp, btc, eur
) -> None:
    """Every flow is counted once in its period and shared out whole among categories.
    Moves to or from a hidden account are left out of this data: they count in the totals
    and periods but not in categories."""
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    thirds = tuple(CategorySplit(name, Decimal("-1")) for name in ("a", "b", "c"))
    await move("-10", date(2026, 10, 3), category="groceries", category_splits=thirds)
    await move("2000", date(2026, 10, 28), category="income", payee="Employer")
    owned = await move("-25", date(2026, 11, 1), category="shopping", category_splits=thirds)
    await run(set_owner_category, [owned.id], "gifts")
    await move("-7", date(2026, 11, 3))
    await move("-40", date(2026, 11, 4), include_in_spending=False)
    await _dividend(run, bank, unassigned, gbp)
    day = date(2026, 11, 5)
    await run(
        record_price,
        instrument_id=btc.id,
        quote_instrument_id=eur.id,
        price=Decimal("50000"),
        as_of_date=day,
    )
    await run(
        record_fx_rate,
        base_instrument_id=eur.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.85"),
        as_of_date=day,
    )
    await _reward(run, wallet, unassigned, btc, "0.0001", day, income_kind="reward")
    await _reward(run, wallet, unassigned, btc, "-0.00002", day, income_kind="reward")

    flow = await cash_flow(session, START, END)

    zero = Decimal(0)
    assert (flow.income, flow.spending) == (Decimal("2014.25"), Decimal("44.35"))
    assert sum((p.income for p in flow.periods), zero) == flow.income
    assert sum((p.spending for p in flow.periods), zero) == flow.spending
    assert sum((c.income for c in flow.categories), zero) == flow.income
    assert sum((c.spending for c in flow.categories), zero) == flow.spending


async def test_amounts_at_an_inverse_rate_still_add_up(
    session, run, move, make_account, unassigned, btc, eur, gbp
) -> None:
    """Only GBP to EUR is recorded, so EUR to GBP is its inverse, with as many digits as
    Postgres gives a division; times a coin's eight places and a price it is past Decimal's
    28. Each amount is rounded once, so periods and categories still add up exactly."""
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    for day in (date(2026, 10, 1), date(2026, 11, 1)):
        await run(
            record_fx_rate,
            base_instrument_id=gbp.id,
            quote_instrument_id=eur.id,
            rate=Decimal("1.1734"),
            as_of_date=day,
        )
        await run(
            record_price,
            instrument_id=btc.id,
            quote_instrument_id=eur.id,
            price=Decimal("50123.47"),
            as_of_date=day,
        )
    thirds = tuple(CategorySplit(name, Decimal("-1")) for name in ("a", "b", "c"))
    for day in (date(2026, 10, 3), date(2026, 11, 3)):
        await move("-10.07", day, instrument=eur, category_splits=thirds)
        await move("1234.56", day, instrument=eur, category="salary")
        for quantity in ("0.96843464", "0.30703946", "0.79343271", "-0.00012347"):
            await _reward(run, wallet, unassigned, btc, quantity, day, income_kind="reward")

    flow = await cash_flow(session, START, END)

    zero = Decimal(0)
    assert flow.missing_count == 0
    assert sum((p.income for p in flow.periods), zero) == flow.income
    assert sum((p.spending for p in flow.periods), zero) == flow.spending
    assert sum((c.income for c in flow.categories), zero) == flow.income
    assert sum((c.spending for c in flow.categories), zero) == flow.spending
