import asyncio
import dataclasses
import importlib.util
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import exc, text

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance import ledger
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ledger import (
    CategorySplit,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    post_transaction,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.valuation import balances_asof, net_worth_series

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)


async def _connection(run):
    return await run(create_connection, connector="monzo", display_name="m", config={})


def _tx(connection_id, a, b, gbp, amount: str, description: str = "Coffee", **kw):
    return TransactionInput(
        occurred_at=WHEN,
        description=description,
        origin="connector",
        connection_id=connection_id,
        external_id="tx_1",
        postings=[
            PostingInput(a.id, gbp.id, Decimal(f"-{amount}")),
            PostingInput(b.id, gbp.id, Decimal(amount)),
        ],
        **kw,
    )


async def test_upsert_creates_then_unchanged(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = _tx(conn.id, a, b, gbp, "5.00")
    created, outcome = await run(upsert_connector_transaction, tx)
    assert outcome == "created"
    events = await session.scalar(text("select count(*) from core.events"))
    same, outcome = await run(upsert_connector_transaction, tx)
    assert outcome == "unchanged"
    assert same.id == created.id
    assert await session.scalar(text("select count(*) from core.events")) == events
    # the command's own audit row is still written, but it records no change
    changes = await session.scalar(
        text("select changes from core.audit_log order by id desc limit 1")
    )
    assert changes == {}


async def test_upsert_replaces_postings_in_place(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    monzo = await make_account(name="Monzo", balance_mode="transactions")
    other = await make_account(name="Other", ledger_type="equity", subtype="other")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, other, monzo, gbp, "5.00"))
    updated, outcome = await run(
        upsert_connector_transaction,
        _tx(conn.id, other, monzo, gbp, "7.50", description="Coffee and cake"),
    )
    assert outcome == "updated"
    assert updated.id == created.id
    assert updated.description == "Coffee and cake"
    old_ids = {p.id for p in created.postings}
    remaining = {r.id for r in await session.execute(text("select id from finance.postings"))}
    assert not old_ids & remaining
    assert await session.scalar(text("select count(*) from finance.transactions")) == 1
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 3))}
    assert rows[monzo.id] == Decimal("7.50")
    updates = await session.scalar(
        text("select count(*) from core.events where type = 'finance.transaction.updated'")
    )
    assert updates == 1


async def test_upsert_refuses_manual_rows(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    manual = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="manual",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
            connection_id=conn.id,
            external_id="tx_1",
        ),
    )
    with pytest.raises(FinanceValidationError):
        await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "2.00"))
    row = await session.scalar(
        text("select description from finance.transactions where id = :i"), {"i": manual.id}
    )
    assert row == "manual"


async def test_refs_must_be_paired(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    with pytest.raises(exc.IntegrityError):
        await session.execute(
            text(
                "insert into finance.transactions"
                " (occurred_at, booking_date, status, origin, owner_entity_id, connection_id)"
                " select now(), current_date, 'booked', 'connector', id, :c"
                " from core.entities limit 1"
            ),
            {"c": conn.id},
        )


async def test_system_accounts_reused_and_excluded_from_net_worth(
    session, run, make_account, gbp
) -> None:
    unassigned = await run(get_or_create_system_account, "unassigned")
    assert await run(get_or_create_system_account, "unassigned") == unassigned
    opening = await run(get_or_create_system_account, "opening_balances")
    assert opening != unassigned
    row = (
        await session.execute(
            text("select ledger_type, subtype, balance_mode from finance.accounts where id = :i"),
            {"i": unassigned},
        )
    ).one()
    assert tuple(row) == ("equity", "other", "transactions")
    monzo = await make_account(name="Monzo", balance_mode="transactions")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="opening",
            postings=[
                PostingInput(monzo.id, gbp.id, Decimal("100")),
                PostingInput(opening, gbp.id, Decimal("-100")),
            ],
        ),
    )
    [point] = await net_worth_series(session, date(2026, 11, 3), date(2026, 11, 3))
    assert point.value == Decimal("100")


async def test_owner_void_is_sticky(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    await run(void_transaction, created.id)
    record, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "9.00", status="booked")
    )
    assert outcome == "unchanged"
    assert record.status == "void"
    status = await session.scalar(
        text("select status from finance.transactions where id = :i"), {"i": created.id}
    )
    assert status == "void"
    quantities = {
        r.quantity
        for r in await session.execute(
            text("select quantity from finance.postings where transaction_id = :i"),
            {"i": created.id},
        )
    }
    assert quantities == {Decimal("5.00"), Decimal("-5.00")}
    updates = await session.scalar(
        text("select count(*) from core.events where type = 'finance.transaction.updated'")
    )
    assert updates == 0


async def test_a_connector_void_gives_way_to_upstream_data(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    # The connector voided it (a pending payment that came back reversed)...
    async with command_context(session, Actor("connector", "monzo"), "sync", {}) as ctx:
        await void_transaction(ctx, created.id)
    # ...and the upstream now reports it again: the upstream wins over the connector's void.
    record, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "9.00", status="booked")
    )

    assert (outcome, record.status) == ("updated", "booked")
    voided_by = await session.scalar(
        text("select voided_by from finance.transactions where id = :i"), {"i": created.id}
    )
    assert voided_by is None


async def test_voids_record_who_made_them(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))

    await run(void_transaction, created.id)  # the owner, through `run`'s user actor

    voided_by = await session.scalar(
        text("select voided_by from finance.transactions where id = :i"), {"i": created.id}
    )
    assert voided_by == "user"


async def test_the_first_void_stands(session, run, make_account, gbp) -> None:
    # The owner voids; a connector acting on an older reading voids it again.
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    await run(void_transaction, created.id)
    async with command_context(session, Actor("connector", "monzo"), "sync", {}) as ctx:
        await void_transaction(ctx, created.id)

    # Still the owner's, so a later sync can't undo it.
    _, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "9.00", status="booked")
    )
    assert outcome == "unchanged"
    voided_by = await session.scalar(
        text("select voided_by from finance.transactions where id = :i"), {"i": created.id}
    )
    assert voided_by == "user"


async def test_an_owner_void_made_during_the_upsert_is_kept(
    session, run, make_account, gbp, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    validate = ledger._validate_transaction

    async def owner_voids_meanwhile(*args, **kwargs):
        # Between the upsert's read and its UPDATE, as a concurrent request would.
        async with command_context(session, Actor("user", "owner"), "void", {}) as ctx:
            await void_transaction(ctx, created.id)
        return await validate(*args, **kwargs)

    monkeypatch.setattr(ledger, "_validate_transaction", owner_voids_meanwhile)
    _, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "9.00", status="booked")
    )

    assert outcome == "unchanged"
    row = (
        await session.execute(
            text("select status, voided_by from finance.transactions where id = :i"),
            {"i": created.id},
        )
    ).one()
    assert (row.status, row.voided_by) == ("void", "user")


async def test_concurrent_callers_share_one_system_account(
    backend_url: str, owner_url: str, migrated: None
) -> None:
    # Two connections syncing at once both ask for "Unassigned" before either commits: on
    # PostgreSQL, with real commits, they must get the same account.
    key = "finance.system_account.unassigned"
    engine, owner = create_engine(backend_url), create_engine(owner_url)
    sessions = make_sessionmaker(engine)
    actor = Actor("connector", "test")

    async def ask() -> uuid.UUID:
        async with sessions() as s, command_context(s, actor, "test.shared_row", {}) as ctx:
            account_id = await get_or_create_system_account(ctx, "unassigned")
            # Still uncommitted while the other asks, as in a sync's transaction.
            await asyncio.sleep(0.5)
            return account_id

    try:
        async with owner.connect() as conn:
            assert (
                await conn.scalar(
                    text("select count(*) from core.settings where key = :k"), {"k": key}
                )
                == 0
            )
        first, second = await asyncio.gather(ask(), ask())
        assert first == second
        async with owner.connect() as conn:
            names = await conn.scalar(
                text("select count(*) from finance.accounts where name = 'Unassigned'")
            )
        assert names == 1
    finally:
        async with owner.begin() as conn:
            ids = [
                r[0]
                for r in await conn.execute(
                    text("select id from finance.accounts where name = 'Unassigned'")
                )
            ]
            await conn.execute(text("delete from core.settings where key = :k"), {"k": key})
            await conn.execute(
                text("delete from core.events where subject_id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(
                text("delete from finance.accounts where id = any(:ids)"), {"ids": ids}
            )
            await conn.execute(text("delete from core.audit_log where command = 'test.shared_row'"))
        await engine.dispose()
        await owner.dispose()


def _migration(name: str) -> Any:
    path = Path(__file__).parents[2] / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_voids_from_before_0012_get_who_made_them_from_the_audit_log(
    session, run, make_account, gbp
) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    reversed_, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    owners = await run(
        upsert_connector_transaction,
        dataclasses.replace(_tx(conn.id, a, b, gbp, "7.00"), external_id="tx_2"),
    )
    async with command_context(session, Actor("connector", "monzo"), "sync", {}) as ctx:
        await void_transaction(ctx, reversed_.id)
    await run(void_transaction, owners[0].id)
    # As the voids were before 0012 recorded who made them.
    await session.execute(text("update finance.transactions set voided_by = null"))

    await session.execute(text(_migration("0012_finance_voided_by.py").BACKFILL_VOIDED_BY))

    rows = dict(
        (await session.execute(text("select id, voided_by from finance.transactions"))).all()
    )
    assert (rows[reversed_.id], rows[owners[0].id]) == ("connector", "user")


def _details(usd, **kw) -> TransactionDetails:
    values: dict[str, Any] = {
        "original_quantity": Decimal("6.50"),
        "original_instrument_id": usd.id,
        "category": "eating_out",
        "labels": ("work", "lunch"),
        "include_in_spending": True,
        "payment_scheme": "mastercard",
        "category_splits": (
            CategorySplit("eating_out", Decimal("3.00")),
            CategorySplit("groceries", Decimal("2.00")),
        ),
    }
    return TransactionDetails(**(values | kw))


async def test_details_are_written_and_loaded(session, run, make_account, gbp, usd) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    details = _details(usd)
    record, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00", details=details)
    )
    assert outcome == "created"
    assert record.details == details
    row = (
        await session.execute(
            text(
                "select original_quantity, category, labels, include_in_spending, payment_scheme"
                " from finance.transactions where id = :i"
            ),
            {"i": record.id},
        )
    ).one()
    assert (row.original_quantity, row.category, row.labels) == (
        Decimal("6.50"),
        "eating_out",
        ["work", "lunch"],
    )
    assert (row.include_in_spending, row.payment_scheme) == (True, "mastercard")
    splits = (
        await session.execute(
            text(
                "select position, category, quantity from finance.transaction_category_splits"
                " where transaction_id = :i order by position"
            ),
            {"i": record.id},
        )
    ).all()
    assert [tuple(r) for r in splits] == [
        (0, "eating_out", Decimal("3.00")),
        (1, "groceries", Decimal("2.00")),
    ]
    plain = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="plain",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    assert plain.details == TransactionDetails()


async def test_changed_details_update_in_place(session, run, make_account, gbp, usd) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00", details=_details(usd))
    )
    changed = _details(
        usd,
        category="groceries",
        category_splits=(CategorySplit("groceries", Decimal("5.00")),),
    )
    updated, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00", details=changed)
    )
    assert outcome == "updated"
    assert updated.id == created.id
    assert updated.details == changed
    assert await session.scalar(text("select count(*) from finance.transactions")) == 1
    assert (
        await session.scalar(text("select count(*) from finance.transaction_category_splits")) == 1
    )
    cleared, outcome = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    assert outcome == "updated"
    assert cleared.details == TransactionDetails()
    assert (
        await session.scalar(text("select count(*) from finance.transaction_category_splits")) == 0
    )


async def test_same_details_are_unchanged(session, run, make_account, gbp, usd) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = _tx(conn.id, a, b, gbp, "5.00", details=_details(usd))
    await run(upsert_connector_transaction, tx)
    events = await session.scalar(text("select count(*) from core.events"))
    _, outcome = await run(upsert_connector_transaction, tx)
    assert outcome == "unchanged"
    assert await session.scalar(text("select count(*) from core.events")) == events


async def test_original_amount_needs_both_fields(run, make_account, gbp, usd) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    for bad in (
        TransactionDetails(original_quantity=Decimal("6.50")),
        TransactionDetails(original_instrument_id=usd.id),
    ):
        with pytest.raises(FinanceValidationError):
            await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00", details=bad))
    nan_split = TransactionDetails(category_splits=(CategorySplit("x", Decimal("NaN")),))
    with pytest.raises(FinanceValidationError):
        await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00", details=nan_split))


def _cancelling(connection_id, a, b, gbp, external_id: str, cancels, amount: str = "5.00"):
    return dataclasses.replace(
        _tx(connection_id, a, b, gbp, amount),
        external_id=external_id,
        cancels_transaction_id=cancels,
    )


async def test_a_connector_transaction_records_what_it_cancels(
    session, run, make_account, gbp
) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    first, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    second, _ = await run(
        upsert_connector_transaction, _cancelling(conn.id, b, a, gbp, "tx_2", first.id)
    )
    assert first.cancels_transaction_id is None
    assert second.cancels_transaction_id == first.id
    assert (await ledger._load_transaction(session, second.id)).cancels_transaction_id == first.id


async def test_the_cancellation_link_is_replaced_in_place(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    first, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    linked = _cancelling(conn.id, b, a, gbp, "tx_2", first.id)
    second, _ = await run(upsert_connector_transaction, linked)
    unlinked, outcome = await run(
        upsert_connector_transaction, dataclasses.replace(linked, cancels_transaction_id=None)
    )
    assert outcome == "updated"
    assert unlinked.id == second.id
    assert unlinked.cancels_transaction_id is None
    _, outcome = await run(
        upsert_connector_transaction, dataclasses.replace(linked, cancels_transaction_id=None)
    )
    assert outcome == "unchanged"
    relinked, outcome = await run(upsert_connector_transaction, linked)
    assert (outcome, relinked.cancels_transaction_id) == ("updated", first.id)


async def test_a_transaction_cannot_cancel_one_that_is_not_there(run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    with pytest.raises(FinanceValidationError):
        await run(
            upsert_connector_transaction,
            _cancelling(conn.id, a, b, gbp, "tx_2", uuid.uuid4()),
        )


async def test_a_void_cancellation_holds_nothing(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    first, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    voided, _ = await run(
        upsert_connector_transaction, _cancelling(conn.id, b, a, gbp, "tx_2", first.id)
    )
    await run(void_transaction, voided.id)
    other, _ = await run(
        upsert_connector_transaction, _cancelling(conn.id, b, a, gbp, "tx_3", first.id)
    )
    assert other.cancels_transaction_id == first.id
    # The void one keeps its link as it was voided.
    kept = await session.scalar(
        text("select cancels_transaction_id from finance.transactions where id = :i"),
        {"i": voided.id},
    )
    assert kept == first.id


async def test_a_transaction_is_cancelled_by_at_most_one(run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    first, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    await run(upsert_connector_transaction, _cancelling(conn.id, b, a, gbp, "tx_2", first.id))
    with pytest.raises(FinanceValidationError):
        await run(upsert_connector_transaction, _cancelling(conn.id, b, a, gbp, "tx_3", first.id))
