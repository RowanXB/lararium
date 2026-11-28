from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from lararium.modules.finance.cashflow import cash_flow
from lararium.modules.finance.instruments import set_hidden
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    post_transaction,
)
from lararium.modules.finance.owner_categories import known_owner_categories, set_owner_category
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.spending import spending_report
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
        keys = ("category", "income_kind", "refund", "include_in_spending")
        details = {k: more.pop(k) for k in keys if k in more}
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
    await post(bank, "2000", date(2026, 10, 28), category="income")  # not spending
    shop = await post(bank, "-30", date(2026, 10, 30), category="groceries", payee="Corner Shop")
    train = await post(bank, "-12", date(2026, 11, 3), category="transport")
    other = await post(bank, "-8", date(2026, 11, 3))
    await run(set_owner_category, [other.id], "gift")
    # A coin spent with no price: listed without an amount, counted as missing, in no total.
    coin = await post(wallet, "-0.0001", date(2026, 11, 4), instrument=btc, income_kind="reward")
    await post(wallet, "-5", date(2026, 11, 5))  # in the wallet account, no category

    report = await spending_report(session, START, END)

    assert (report.total, report.missing_count) == (Decimal("55"), 1)
    assert [e.booking_date for e in report.entries] == sorted(
        (e.booking_date for e in report.entries), reverse=True
    )
    [unpriced] = [e for e in report.entries if e.transaction_id == coin.id]
    assert (unpriced.amount, unpriced.quantity, unpriced.instrument) == (
        None,
        Decimal("0.0001"),
        "BTC",
    )
    [paid] = [e for e in report.entries if e.transaction_id == shop.id]
    assert (paid.amount, paid.quantity, paid.counterparty_name, paid.refund) == (
        Decimal("30"),
        Decimal("30"),
        "Corner Shop",
        False,
    )
    assert [(c.category, c.amount) for c in report.categories] == [
        ("groceries", Decimal("30")),
        ("transport", Decimal("12")),
        ("gift", Decimal("8")),
        (None, Decimal("5")),
    ]
    assert [(p.start, p.total) for p in report.periods] == [
        (date(2026, 10, 1), Decimal("30")),
        (date(2026, 11, 1), Decimal("25")),
    ]
    assert report.known_categories == ["gift"]

    by_account = await spending_report(session, START, END, account_id=wallet.id)
    assert (by_account.total, by_account.missing_count) == (Decimal("5"), 1)
    assert [e.account_name for e in by_account.entries] == ["Exchange", "Exchange"]

    only = await spending_report(session, START, END, category="transport")
    assert [e.transaction_id for e in only.entries] == [train.id]
    assert only.total == Decimal("55")  # a filter narrows the entries, never the totals
    none = await spending_report(session, START, END, category="uncategorised")
    assert Decimal("5") in [e.amount for e in none.entries]
    assert shop.id not in [e.transaction_id for e in none.entries]


async def test_a_refund_is_negative_spending(session, make_account, post) -> None:
    """A refund is an entry with a negative amount and quantity, in the cash flow's spending
    too; a payment the source marks as not spending is no entry."""
    bank = await make_account(name="Bank", balance_mode="transactions")
    await post(bank, "500", date(2026, 11, 2), payee="Employer")  # income
    await post(bank, "-40", date(2026, 11, 3), category="shopping", payee="Shop")
    await post(bank, "15", date(2026, 11, 4), category="shopping", payee="Shop", refund=True)
    await post(bank, "-200", date(2026, 11, 5), include_in_spending=False)

    report = await spending_report(session, START, END, "month", ())
    flow = await cash_flow(session, START, END, "month", ())

    assert [(e.amount, e.quantity, e.refund) for e in report.entries] == [
        (Decimal("-15"), Decimal("-15"), True),
        (Decimal("40"), Decimal("40"), False),
    ]
    assert [(c.category, c.amount) for c in report.categories] == [("shopping", Decimal("25"))]
    assert report.total == flow.spending == Decimal("25")


async def test_owner_category_beats_source_category(session, run, make_account, post) -> None:
    bank = await make_account(name="Bank", balance_mode="transactions")
    record = await post(bank, "-9", date(2026, 11, 3), category="eating_out")

    await run(set_owner_category, [record.id], "work lunch")
    [entry] = (await spending_report(session, START, END)).entries
    assert (entry.category, entry.owner_category, entry.source_category) == (
        "work_lunch",
        "work_lunch",
        "eating_out",
    )

    await run(set_owner_category, [record.id], None)
    [entry] = (await spending_report(session, START, END)).entries
    assert (entry.category, entry.owner_category, entry.source_category) == (
        "eating_out",
        None,
        "eating_out",
    )


async def test_known_categories_are_the_spending_sides(
    session, run, make_account, post, usd
) -> None:
    bank = await make_account(name="Bank", balance_mode="transactions")
    salary = await post(bank, "100", date(2026, 11, 2))
    shop = await post(bank, "-10", date(2026, 11, 3))
    # The source leaves it out of spending, so the page never lists it.
    moved = await post(bank, "-20", date(2026, 11, 4), include_in_spending=False)
    # In a currency the owner hid, so no report counts it.
    abroad = await post(bank, "-30", date(2026, 11, 5), instrument=usd)
    await run(set_owner_category, [salary.id], "salary")
    await run(set_owner_category, [shop.id], "groceries")
    await run(set_owner_category, [moved.id], "savings")
    await run(set_owner_category, [abroad.id], "travel")
    await run(set_hidden, usd.id, True)

    assert (await spending_report(session, START, END)).known_categories == ["groceries"]
    assert await known_owner_categories(session, "income", ()) == ["salary"]


async def test_a_move_to_a_hidden_account_is_an_entry(session, run, make_account, gbp) -> None:
    """Money moved to a hidden pot is spending naming the pot, even when the source leaves
    it out of spending, and the owner can categorise it. Shown again, the pot makes the move
    a transfer between own accounts, no entry, while the owner's category is kept for when
    it is hidden again."""
    bank = await make_account(name="Bank", balance_mode="transactions")
    pot = await make_account(name="Rainy day pot", subtype="savings_pot", balance_mode="hybrid")
    moved = await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 11, 2, 12, tzinfo=LON),
            description="Rainy day pot",
            postings=[
                PostingInput(bank.id, gbp.id, Decimal("-75")),
                PostingInput(pot.id, gbp.id, Decimal("75")),
            ],
            details=TransactionDetails(category="savings", include_in_spending=False),
        ),
    )
    hidden = [pot.id]

    [entry] = (await spending_report(session, START, END, "month", hidden)).entries
    assert (
        entry.transaction_id,
        entry.account_id,
        entry.amount,
        entry.category,
        entry.hidden_account_name,
        entry.refund,
    ) == (moved.id, bank.id, Decimal("75"), "savings", "Rainy day pot", False)

    await run(set_owner_category, [moved.id], "saving up")
    report = await spending_report(session, START, END, "month", hidden)
    assert [(e.category, e.source_category) for e in report.entries] == [("saving_up", "savings")]
    assert report.total == (await cash_flow(session, START, END, "month", hidden)).spending
    assert report.known_categories == ["saving_up"]
    assert await known_owner_categories(session, "income", hidden) == []

    shown = await spending_report(session, START, END, "month", ())
    assert (shown.entries, shown.total) == ([], Decimal("0"))
    assert shown.known_categories == []
    assert (await cash_flow(session, START, END, "month", ())).spending == 0
    assert (
        await session.scalar(text("select category from finance.transaction_owner_categories"))
        == "saving_up"
    )

    [again] = (await spending_report(session, START, END, "month", hidden)).entries
    assert (again.category, again.amount) == ("saving_up", Decimal("75"))


OWNER_CATEGORIES = [None, "groceries", "gift"]


@st.composite
def moves(draw):
    """Signed fiat flows on two accounts, some with the source's category, some a refund or
    marked as not spending, some with the owner's category; a coin spent, priced or not; and
    one move to a hidden pot, which the source may mark as not spending, with the owner's
    category or not."""
    return {
        "flows": draw(
            st.lists(
                st.tuples(
                    st.sampled_from(["A", "B"]),
                    st.integers(-5000, 5000).filter(bool),
                    st.integers(1, 60),
                    st.sampled_from([None, "shopping", "transport"]),
                    st.sampled_from(OWNER_CATEGORIES),
                    st.booleans(),  # a refund, when positive
                    st.sampled_from([None, None, False]),  # include_in_spending
                ),
                max_size=8,
            )
        ),
        # (satoshis, day offset, a GBP price or None for none, the owner's category)
        "coin": draw(
            st.none()
            | st.tuples(
                st.integers(1, 100_000),
                st.integers(1, 60),
                st.none() | st.integers(1_000, 90_000),
                st.sampled_from(OWNER_CATEGORIES),
            )
        ),
        "pot": draw(st.integers(1, 900)),
        "pot_counted": draw(st.sampled_from([None, False])),
        "pot_owner": draw(st.sampled_from(OWNER_CATEGORIES)),
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
        for which, pence, offset, category, owner, refund, counted in scenario["flows"]:
            day = date.fromordinal(START.toordinal() + offset)
            extra = {"category": category} if category else {}
            if refund and pence > 0:
                extra["refund"] = True
            if counted is not None:
                extra["include_in_spending"] = counted
            record = await post(accounts[which], str(Decimal(pence) / 100), day, **extra)
            if owner:
                await run(set_owner_category, [record.id], owner)
        if scenario["coin"] is not None:
            satoshis, offset, price, owner = scenario["coin"]
            if price is not None:
                await run(
                    record_price,
                    instrument_id=btc.id,
                    quote_instrument_id=gbp.id,
                    price=Decimal(price),
                    as_of_date=START,
                )
            # A coin leaving as a stated reversal of a reward: spending, as money would be.
            coin = await post(
                wallet,
                str(-Decimal(satoshis).scaleb(-8)),
                date.fromordinal(START.toordinal() + offset),
                instrument=btc,
                income_kind="reward",
            )
            if owner:
                await run(set_owner_category, [coin.id], owner)
        # Money moved out to a hidden pot: spending, an entry like any other, whatever the
        # source says of it.
        pot_move = await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(2026, 11, 10, 12, tzinfo=LON),
                description="",
                postings=[
                    PostingInput(a.id, gbp.id, -Decimal(scenario["pot"])),
                    PostingInput(pot.id, gbp.id, Decimal(scenario["pot"])),
                ],
                details=TransactionDetails(include_in_spending=scenario["pot_counted"]),
            ),
        )
        if scenario["pot_owner"]:
            await run(set_owner_category, [pot_move.id], scenario["pot_owner"])
        excluded = [pot.id]
        for bucket in ("day", "week", "month"):
            report = await spending_report(session, START, END, bucket, excluded)
            flow = await cash_flow(session, START, END, bucket, excluded)
            assert report.total == flow.spending
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
                c.category: c.spending for c in flow.categories if c.spending
            }
