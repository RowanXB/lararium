"""A manual hybrid account's history starts at its first snapshot in each instrument, as a
connected account's starts at its first record (design §5.2): a transfer whose record is at
or before that snapshot's as_of is already inside it (valuation's own cut-off), so it is
settled against Opening balances, not given a placeholder that valuation would sum, from
zero, on every day before the snapshot."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from lararium.modules.finance.accounts import create_account, set_balance_mode
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    post_transaction,
    upsert_connector_transaction,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.transfers import list_transfers, transfer_contra
from lararium.modules.finance.valuation import balances_asof, net_worth_series

from .conftest import (
    B_IDENTIFIER,
    WHEN,
    assert_consistent,
    balances_on,
    placeholders,
    postings_of,
)

OPENING_DAY = WHEN.date()
TODAY = OPENING_DAY + timedelta(days=10)
# Days before WHEN the transfers are sent: long before; inside the window before the
# snapshot (the case a window rule would miss); exactly at it; and after it.
OFFSETS = {"long_before": timedelta(days=-200), "just_before": timedelta(days=-3)}
FIRST_DAY = OPENING_DAY - timedelta(days=201)


async def _manual(run, gbp, mode: str):
    """B with an opening snapshot of 1000 at WHEN: created hybrid, or created snapshots and
    then switched to hybrid (as an existing statement account is)."""
    b = await run(
        create_account,
        name="B",
        ledger_type="asset",
        subtype="bank",
        balance_mode="hybrid" if mode == "hybrid" else "snapshots",
    )
    await run(
        record_snapshot, account_id=b.id, instrument_id=gbp.id, quantity=Decimal(1000), as_of=WHEN
    )
    if mode == "switched":
        b = await run(set_balance_mode, b.id, "hybrid")
    return b


async def _history(session, account_id) -> tuple[list[Decimal], list[Decimal | None]]:
    """Net worth, and the account's balance, on every day before the opening snapshot."""
    days = [FIRST_DAY + timedelta(days=n) for n in range((OPENING_DAY - FIRST_DAY).days)]
    worth = [point.value for point in await net_worth_series(session, days[0], days[-1])]
    balance: list[Decimal | None] = []
    for day in days:
        found = {r.account_id: r.quantity for r in await balances_asof(session, day)}
        balance.append(found.get(account_id))
    return worth, balance


async def _net_worth(session, day: date) -> Decimal:
    [point] = await net_worth_series(session, day, day)
    return point.value


@pytest.fixture(params=["hybrid", "switched"])
async def opened(request, session, run, ids, gbp, connected_bank, connector_tx, heal):
    """A sends 100 to B at WHEN-200d, WHEN-3d, WHEN (B's opening snapshot) and WHEN+1d;
    then B's identifier is registered. Returns A, B, the records by name, and net worth and
    B's balance before WHEN as they were before the registration."""
    a = await connected_bank("A")
    b = await _manual(run, gbp, request.param)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    at = {
        "long_before": WHEN + OFFSETS["long_before"],
        "just_before": WHEN + OFFSETS["just_before"],
        "at_snapshot": WHEN,
        "after": WHEN + timedelta(days=1),
    }
    records = {
        name: await connector_tx(
            a, "-100", external_id=name, occurred_at=when, identifier_id=identifier
        )
        for name, when in at.items()
    }
    await heal()
    before = await _history(session, b.id)
    worth_today = await _net_worth(session, TODAY)
    await run(assign_identifier, b.id, "uk_bank_account", B_IDENTIFIER)
    await heal()
    return a, b, records, before, worth_today


def _by_record(transfers):
    return {t.out_transaction_id: t for t in transfers}


async def test_transfers_at_or_before_the_opening_snapshot_are_settled(
    session, ids, opened
) -> None:
    _, b, records, before, worth_today = opened
    transfers = _by_record(await list_transfers(session))
    for name in ("long_before", "just_before", "at_snapshot"):
        transfer = transfers[records[name].id]
        assert (transfer.status, transfer.placeholder_transaction_id) == ("before_history", None)
        assert await postings_of(session, transfer.settlement_transaction_id) == {
            (ids.in_transit, Decimal(-100)),
            (ids.opening_balances, Decimal(100)),
        }
    after = transfers[records["after"].id]
    assert (after.status, after.settlement_transaction_id) == ("inferred", None)
    [placeholder] = await placeholders(session)
    assert after.placeholder_transaction_id == placeholder.id

    # Nothing before the snapshot changed: B is still absent, net worth as it was.
    assert await _history(session, b.id) == before
    # Today: the snapshot plus the one transfer after it, which registering adds to B.
    assert (await balances_on(session, TODAY))[b.id] == Decimal(1100)
    assert await _net_worth(session, TODAY) == worth_today + 100
    await assert_consistent(session)


async def test_an_earlier_snapshot_reopens_what_it_no_longer_contains(
    session, run, gbp, opened, heal
) -> None:
    _, b, records, *_ = opened
    await run(
        record_snapshot,
        account_id=b.id,
        instrument_id=gbp.id,
        quantity=Decimal(800),
        as_of=WHEN - timedelta(days=10),
    )
    assert await heal() == 2

    transfers = _by_record(await list_transfers(session))
    expected = {
        "long_before": "before_history",
        "just_before": "inferred",
        "at_snapshot": "inferred",
        "after": "inferred",
    }
    for name, status in expected.items():
        transfer = transfers[records[name].id]
        assert transfer.status == status
        assert (transfer.placeholder_transaction_id is not None) == (status == "inferred")
        assert (transfer.settlement_transaction_id is not None) == (status == "before_history")
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_later_real_record_replaces_the_settlement(
    session, run, ids, gbp, opened, heal
) -> None:
    _, b, records, *_ = opened
    sent = records["long_before"]
    contra = await transfer_contra(
        session, source=None, own_account_id=b.id, counterparty_identifier_id=None, other_postings=0
    )
    row = await run(
        post_transaction,
        TransactionInput(
            occurred_at=sent.occurred_at,
            description="Faster payment from A",
            origin="manual",
            postings=[
                PostingInput(b.id, gbp.id, Decimal(100)),
                PostingInput(contra, gbp.id, Decimal(-100)),
            ],
            counterparty=CounterpartyFields(bank_transfer=True),
        ),
    )
    await heal()

    transfer = _by_record(await list_transfers(session))[sent.id]
    assert (transfer.status, transfer.in_transaction_id, transfer.settlement_transaction_id) == (
        "matched",
        row.id,
        None,
    )
    assert len(await placeholders(session, origin="rule")) == 2  # the other two stay settled
    await assert_consistent(session)
    assert await heal() == 0


async def test_transactions_mode_manual_account_keeps_its_placeholders(
    session, run, gbp, connected_bank, connector_tx, heal
) -> None:
    """Valuation ignores a transactions account's snapshots, so its placeholders are all
    it has: a snapshot on it never settles one."""
    a = await connected_bank("A")
    b = await run(
        create_account, name="B", ledger_type="asset", subtype="bank", balance_mode="transactions"
    )
    await run(
        record_snapshot, account_id=b.id, instrument_id=gbp.id, quantity=Decimal(0), as_of=WHEN
    )
    await run(assign_identifier, b.id, "uk_bank_account", B_IDENTIFIER)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    record = await connector_tx(
        a, "-100", occurred_at=WHEN - timedelta(days=3), identifier_id=identifier
    )
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.out_transaction_id) == ("inferred", record.id)
    assert (await balances_on(session, TODAY))[b.id] == Decimal(100)
    await assert_consistent(session)
    assert await heal() == 0


async def test_a_snapshot_in_another_currency_settles_nothing(
    session, run, ids, gbp, usd, connected_bank, manual_bank, heal
) -> None:
    """Snapshots, and valuation, are per instrument: a GBP snapshot contains no USD."""
    a = await connected_bank("A")
    b = await manual_bank("B", snapshot_at=WHEN)  # GBP only
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    contra = await transfer_contra(
        session,
        source=("connection", f"{a.connection_id}:usd_1"),
        own_account_id=a.account.id,
        counterparty_identifier_id=identifier,
        other_postings=0,
    )
    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=WHEN - timedelta(days=3),
            description="Transfer",
            origin="connector",
            connection_id=a.connection_id,
            external_id="usd_1",
            postings=[
                PostingInput(a.account.id, usd.id, Decimal(-100)),
                PostingInput(contra, usd.id, Decimal(100)),
            ],
            counterparty=CounterpartyFields(identifier_id=identifier, bank_transfer=True),
        ),
    )
    await heal()

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.instrument_id) == ("inferred", usd.id)
    usd_balances = {
        r.account_id: r.quantity
        for r in await balances_asof(session, TODAY)
        if r.instrument_id == usd.id
    }
    assert usd_balances[b.id] == Decimal(100)
    await assert_consistent(session)
    assert await heal() == 0
