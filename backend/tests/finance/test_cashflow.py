from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from lararium.modules.finance.accounts import set_excluded_from_reports
from lararium.modules.finance.cashflow import CategoryTotal, PayeeTotal, cash_flow
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ledger import (
    CategorySplit,
    CounterpartyFields,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.pricing import record_fx_rate
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
            for key in ("category", "include_in_spending", "category_splits")
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
