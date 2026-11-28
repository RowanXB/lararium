from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lararium.modules.finance.cashflow import cash_flow
from lararium.modules.finance.income import income_report
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    post_transaction,
)
from lararium.modules.finance.owner_categories import set_owner_category
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.system_accounts import get_or_create_system_account

from .importing.staging_helpers import isolated_example

LON = ZoneInfo("Europe/London")
START, END = date(2026, 10, 1), date(2026, 11, 30)


@pytest.fixture
async def unassigned(run):
    return await run(get_or_create_system_account, "unassigned")


@pytest.fixture
def post(run, unassigned, gbp):
    """Post `amount` to `account` against Unassigned; returns the transaction."""

    async def _post(account, amount: str, day: date, *, instrument=None, **more):
        instrument_id = (instrument or gbp).id
        details = {k: more.pop(k) for k in ("category", "income_kind", "refund") if k in more}
        return await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(day.year, day.month, day.day, 12, tzinfo=LON),
                description=more.pop("description", ""),
                postings=[
                    PostingInput(account.id, instrument_id, Decimal(amount)),
                    PostingInput(unassigned, instrument_id, -Decimal(amount)),
                ],
                counterparty=CounterpartyFields(name=more.pop("payee", None)),
                details=TransactionDetails(**details),
            ),
        )

    return _post


async def test_entries_totals_and_filters(session, run, make_account, post, btc) -> None:
    bank = await make_account(name="Bank", balance_mode="transactions")
    wallet = await make_account(name="Exchange", subtype="wallet", balance_mode="transactions")
    salary = await post(bank, "2000", date(2026, 10, 28), category="income", payee="Employer")
    await post(bank, "-30", date(2026, 10, 30), category="groceries")  # not income
    interest = await post(bank, "1.50", date(2026, 11, 3), income_kind="interest")
    other = await post(bank, "12", date(2026, 11, 3))
    await run(set_owner_category, [other.id], "gift")
    # A reward with no price: listed without an amount, counted as missing, in no total.
    reward = await post(wallet, "0.0001", date(2026, 11, 4), instrument=btc, income_kind="reward")
    await post(wallet, "5", date(2026, 11, 5))  # in the wallet account, no category

    report = await income_report(session, START, END)

    assert (report.total, report.missing_count) == (Decimal("2018.50"), 1)
    assert [e.booking_date for e in report.entries] == sorted(
        (e.booking_date for e in report.entries), reverse=True
    )
    [unpriced] = [e for e in report.entries if e.transaction_id == reward.id]
    assert (unpriced.amount, unpriced.quantity, unpriced.instrument) == (
        None,
        Decimal("0.0001"),
        "BTC",
    )
    assert [(c.category, c.amount) for c in report.categories] == [
        ("income", Decimal("2000")),
        ("gift", Decimal("12")),
        (None, Decimal("5")),
        ("interest", Decimal("1.50")),
    ]
    assert [(p.start, p.total) for p in report.periods] == [
        (date(2026, 10, 1), Decimal("2000")),
        (date(2026, 11, 1), Decimal("18.50")),
    ]
    assert report.known_categories == ["gift"]
    assert report.hidden_moved_in == 0

    by_account = await income_report(session, START, END, account_id=wallet.id)
    assert (by_account.total, by_account.missing_count) == (Decimal("5"), 1)
    assert [e.account_name for e in by_account.entries] == ["Exchange", "Exchange"]

    only = await income_report(session, START, END, category="interest")
    assert [e.transaction_id for e in only.entries] == [interest.id]
    assert only.total == Decimal("2018.50")  # a filter narrows the entries, never the totals
    none = await income_report(session, START, END, category="uncategorised")
    assert {e.description for e in none.entries} == {""}
    assert Decimal("5") in [e.amount for e in none.entries]
    assert salary.id not in [e.transaction_id for e in none.entries]


OWNER_CATEGORIES = [None, "family_money", "gift"]


async def test_a_refund_is_not_income(session, make_account, post) -> None:
    """A refund is negative spending in the cash flow (card-refunds design), so it is no
    entry here, and the page's total still agrees with the Overview's income."""
    bank = await make_account(name="Bank", balance_mode="transactions")
    await post(bank, "500", date(2026, 11, 2), payee="Employer")
    await post(bank, "-40", date(2026, 11, 3), category="shopping", payee="Shop")
    await post(bank, "15", date(2026, 11, 4), category="shopping", payee="Shop", refund=True)

    report = await income_report(session, START, END, "month", ())
    flow = await cash_flow(session, START, END, "month", ())

    assert [e.amount for e in report.entries] == [Decimal("500")]
    assert report.total == flow.income == Decimal("500")
    assert flow.spending == Decimal("25")


@st.composite
def moves(draw):
    """Signed fiat flows on two accounts, some with the source's category, some with the
    owner's; a coin reward, priced or not; and one hidden-pot move."""
    return {
        "flows": draw(
            st.lists(
                st.tuples(
                    st.sampled_from(["A", "B"]),
                    st.integers(-5000, 5000).filter(bool),
                    st.integers(1, 60),
                    st.sampled_from([None, "salary", "gift", "interest"]),
                    st.sampled_from(OWNER_CATEGORIES),
                ),
                max_size=8,
            )
        ),
        # (satoshis, day offset, a GBP price or None for none, the owner's category)
        "reward": draw(
            st.none()
            | st.tuples(
                st.integers(1, 100_000),
                st.integers(1, 60),
                st.none() | st.integers(1_000, 90_000),
                st.sampled_from(OWNER_CATEGORIES),
            )
        ),
        "pot": draw(st.integers(1, 900)),
    }


@settings(max_examples=25)
@given(scenario=moves())
async def test_agrees_with_cash_flow(session, run, make_account, post, gbp, btc, scenario) -> None:
    async with isolated_example(session):
        a = await make_account(name="A", balance_mode="transactions")
        b = await make_account(name="B", balance_mode="transactions")
        wallet = await make_account(name="W", subtype="wallet", balance_mode="transactions")
        pot = await make_account(name="Pot", subtype="savings_pot", balance_mode="hybrid")
        accounts = {"A": a, "B": b}
        for which, pence, offset, category, owner in scenario["flows"]:
            day = date.fromordinal(START.toordinal() + offset)
            record = await post(
                accounts[which],
                str(Decimal(pence) / 100),
                day,
                **({"category": category} if category else {}),
            )
            if owner:
                await run(set_owner_category, [record.id], owner)
        if scenario["reward"] is not None:
            satoshis, offset, price, owner = scenario["reward"]
            if price is not None:
                await run(
                    record_price,
                    instrument_id=btc.id,
                    quote_instrument_id=gbp.id,
                    price=Decimal(price),
                    as_of_date=START,
                )
            reward = await post(
                wallet,
                str(Decimal(satoshis).scaleb(-8)),
                date.fromordinal(START.toordinal() + offset),
                instrument=btc,
                income_kind="reward",
            )
            if owner:
                await run(set_owner_category, [reward.id], owner)
        # Money moved in from a hidden pot: income for the Overview, not an entry here.
        await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(2026, 11, 10, 12, tzinfo=LON),
                description="",
                postings=[
                    PostingInput(pot.id, gbp.id, -Decimal(scenario["pot"])),
                    PostingInput(a.id, gbp.id, Decimal(scenario["pot"])),
                ],
            ),
        )
        excluded = [pot.id]
        for bucket in ("day", "week", "month"):
            report = await income_report(session, START, END, bucket, excluded)
            flow = await cash_flow(session, START, END, bucket, excluded)
            assert report.total + report.hidden_moved_in == flow.income
            assert report.missing_count == flow.missing_count
            assert sum((c.amount for c in report.categories), Decimal(0)) == report.total
            assert sum((p.total for p in report.periods), Decimal(0)) == report.total
            for period in report.periods:
                assert sum((c.amount for c in period.by_category), Decimal(0)) == period.total
            # The owner's category decides each entry it is set on; with no category splits
            # drawn, both reports then agree category by category too.
            for entry in report.entries:
                if entry.owner_category is not None:
                    assert entry.category == entry.owner_category
            assert {c.category: c.amount for c in report.categories} == {
                c.category: c.income for c in flow.categories if c.income
            }
