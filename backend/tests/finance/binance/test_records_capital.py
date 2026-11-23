"""Deposits and withdrawals in the ledger (design 5.7 and 5.12), the flags a run raises, and how
they are published (design 5.13 and 5.14)."""

import uuid
from decimal import Decimal

from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.connections import get_connection
from lararium.core.raw_records import store_raw_record

from .fixtures import BASE_TIME_MS, HOUR_MS, NOW_MS, FakeBinance, build_row
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
    CONNECTOR,
    RecordingNotifier,
    binance_connection,
    later_config,
    run_binance_sync,
)

HASH = "0x" + "AB" * 32


def _deposit(status: int, **fields) -> dict:
    values = {"coin": "AAA", "amount": "5", "walletType": 0, "transferType": 0, "status": status}
    return build_row("deposits", **(values | fields))


def _withdrawal(status: int, **fields) -> dict:
    values = {
        "coin": "AAA",
        "amount": "2",
        "transactionFee": "0.1",
        "walletType": 0,
        "transferType": 0,
        "status": status,
    }
    return build_row("withdrawals", **(values | fields))


def _text_time(ms: int) -> str:
    return at(ms).strftime("%Y-%m-%d %H:%M:%S")


async def test_deposit_posts_when_credited(session, run) -> None:
    connection = await binance_connection(run)
    pending = _deposit(0, completeTime=None)

    await normalize_rows(session, connection.id, ("deposits", [pending]))

    [row] = await typed(session, "binance_deposits", connection.id)
    assert row.status == 0 and row.transaction_id is None
    assert await keys(session, connection.id) == []

    credited = _deposit(1, completeTime=BASE_TIME_MS + HOUR_MS)
    await normalize_rows(session, connection.id, ("deposits", [credited]))

    key = f"deposit:{credited['id']}"
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(5)),
        ("Unassigned", "AAA", Decimal(-5)),
    ]
    found = await transaction(session, key)
    # Credited at completeTime, booked on its UTC date.
    assert found.occurred_at == at(BASE_TIME_MS + HOUR_MS)
    assert (found.occurred_tz, found.booking_date) == ("UTC", found.occurred_at.date())
    [row] = await typed(session, "binance_deposits", connection.id)
    assert (row.status, row.transaction_id) == (1, found.id)
    await assert_balanced_and_quiet(session, connection.id)


async def test_locked_deposit_is_credited(session, run) -> None:
    connection = await binance_connection(run)
    row = _deposit(6)

    await normalize_rows(session, connection.id, ("deposits", [row]))

    assert await holdings(session, connection.id) == {
        ("Binance Spot", "AAA"): Decimal(5),
        ("Unassigned", "AAA"): Decimal(-5),
    }


async def test_deposit_without_completion_time_is_dated_when_inserted(session, run) -> None:
    connection = await binance_connection(run)
    row = _deposit(1, completeTime=None, insertTime=BASE_TIME_MS)

    await normalize_rows(session, connection.id, ("deposits", [row]))

    assert (await transaction(session, f"deposit:{row['id']}")).occurred_at == at(BASE_TIME_MS)


async def test_funding_deposit_lands_in_funding(session, run) -> None:
    connection = await binance_connection(run)
    row = _deposit(1, walletType=1)

    await normalize_rows(session, connection.id, ("deposits", [row]))

    assert plain(await postings(session, f"deposit:{row['id']}"))[0] == (
        "Binance Funding",
        "AAA",
        Decimal(5),
    )
    await assert_balanced_and_quiet(session, connection.id)


async def test_withdrawal_is_two_transactions(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6)

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    key = f"withdrawal:{row['id']}"
    # A transfer leg: its own posting and the contra, nothing else.
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-2)),
        ("Unassigned", "AAA", Decimal(2)),
    ]
    assert [(p[0], p[2], p[7]) for p in await postings(session, f"{key}:fee")] == [
        ("Binance Spot", Decimal("-0.1"), True),
        ("Unassigned", Decimal("0.1"), False),
    ]
    found = await transaction(session, key)
    assert found.occurred_at == at(BASE_TIME_MS)  # applyTime
    [typed_row] = await typed(session, "binance_withdrawals", connection.id)
    assert typed_row.transaction_id == found.id
    await assert_balanced_and_quiet(session, connection.id)


async def test_withdrawal_without_a_fee_has_no_fee_transaction(session, run) -> None:
    connection = await binance_connection(run)
    row = _withdrawal(6, transactionFee="0")

    await normalize_rows(session, connection.id, ("withdrawals", [row]))

    assert await keys(session, connection.id) == [f"withdrawal:{row['id']}"]


async def test_pending_withdrawal_is_posted_once_applied(session, run) -> None:
    connection = await binance_connection(run)
    for status in (0, 2, 4):
        await normalize_rows(session, connection.id, ("withdrawals", [_withdrawal(status)]))
        assert await holdings(session, connection.id) == {
            ("Binance Spot", "AAA"): Decimal("-2.1"),
            ("Unassigned", "AAA"): Decimal("2.1"),
        }
    await assert_balanced_and_quiet(session, connection.id)


async def test_failed_withdrawal_is_refunded_not_voided(session, run) -> None:
    connection = await binance_connection(run)
    processing = _withdrawal(4, completeTime=None)
    await normalize_rows(session, connection.id, ("withdrawals", [processing]))

    # The next sync's overlapping window: the same withdrawal, now failed.
    refunded_ms = BASE_TIME_MS + 2 * HOUR_MS
    failed = _withdrawal(5, completeTime=_text_time(refunded_ms))
    report = await normalize_rows(session, connection.id, ("withdrawals", [failed]))

    key = f"withdrawal:{failed['id']}"
    # Still posted: the balance really fell; no longer a transfer leg.
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-2)),
        ("Unassigned", "AAA", Decimal(2)),
    ]
    assert (await transaction(session, key)).status == "booked"
    assert len(await postings(session, f"{key}:fee")) == 2
    assert plain(await postings(session, f"{key}:refund")) == [
        ("Binance Spot", "AAA", Decimal(2)),
        ("Binance Spot", "AAA", Decimal("0.1")),
        ("Unassigned", "AAA", Decimal("-2.1")),
    ]
    assert (await transaction(session, f"{key}:refund")).occurred_at == at(refunded_ms)
    # Only the refund changed the ledger (the contra was already Unassigned).
    assert {moment for _, _, moment in report.touched} == {at(refunded_ms)}
    assert await holdings(session, connection.id) == {}
    [row] = await typed(session, "binance_withdrawals", connection.id)
    assert row.status == 5
    assert row.transaction_id == (await transaction(session, key)).id
    await assert_balanced_and_quiet(session, connection.id)


async def test_refund_with_no_time_is_dated_when_the_sync_read_the_balances(session, run) -> None:
    connection = await binance_connection(run)
    await normalize_rows(session, connection.id, ("withdrawals", [_withdrawal(4)]))
    failed = _withdrawal(5, completeTime=None)
    read_ms = NOW_MS - HOUR_MS
    sync_id = await _balance_read(session, connection.id, read_ms)
    await ingest(session, connection.id, "withdrawals", [failed], sync_id=sync_id)

    await run_normalize(session, connection.id)

    refund = await transaction(session, f"withdrawal:{failed['id']}:refund")
    assert refund.occurred_at == at(read_ms)


async def _balance_read(session, connection_id, read_from_ms: int):
    """A balance read of a sync, stored as the sync stores it; that sync's id."""
    sync_id = uuid.uuid4()
    async with command_context(session, CONNECTOR, "test", {}) as ctx:
        await store_raw_record(
            ctx,
            connection_id=connection_id,
            record_type="binance.spot_account",
            request={},
            content={"read_from_ms": read_from_ms, "read_to_ms": read_from_ms + 200, "body": {}},
            connector_version="test",
            sync_id=sync_id,
        )
    return sync_id


async def test_withdrawal_cancelled_before_it_was_seen_posts_nothing(session, run) -> None:
    connection = await binance_connection(run)
    rows = [_withdrawal(1), _withdrawal(3, id="w-3"), _withdrawal(5, id="w-5")]

    await normalize_rows(session, connection.id, ("withdrawals", rows))

    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_withdrawals", connection.id)) == 3
    await assert_balanced_and_quiet(session, connection.id)


async def test_onchain_hash_is_typed(session, run) -> None:
    connection = await binance_connection(run)
    onchain = _deposit(1, txId=HASH)
    internal = _deposit(1, id="2001", txId="Off-chain transfer 123", transferType=1)
    sent = _withdrawal(6, txId="CD" * 32)

    await normalize_rows(
        session, connection.id, ("deposits", [onchain, internal]), ("withdrawals", [sent])
    )

    found = {
        row.id: (row.tx_id, row.tx_hash, row.is_onchain)
        for row in await typed(session, "binance_deposits", connection.id)
    }
    assert found[onchain["id"]] == (HASH, HASH.lower(), True)
    assert found["2001"] == ("Off-chain transfer 123", None, False)
    [withdrawn] = await typed(session, "binance_withdrawals", connection.id)
    assert (withdrawn.tx_hash, withdrawn.is_onchain) == ("cd" * 32, True)


async def test_unknown_status_is_typed_flagged_and_not_posted(session, run) -> None:
    connection = await binance_connection(run)

    report = await normalize_rows(
        session,
        connection.id,
        ("deposits", [_deposit(9)]),
        ("withdrawals", [_withdrawal(7)]),
    )

    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_deposits", connection.id)) == 1
    assert len(await typed(session, "binance_withdrawals", connection.id)) == 1
    assert report.new_flags == ["deposits.status=9", "withdrawals.status=7"]
    after = await get_connection(session, connection.id)
    assert after.config["flags"] == ["deposits.status=9", "withdrawals.status=7"]
    assert "Binance flagged: deposits.status=9" in after.warnings
    await assert_balanced_and_quiet(session, connection.id)


async def test_unknown_wallet_type_is_flagged_and_not_posted(session, run) -> None:
    connection = await binance_connection(run)

    report = await normalize_rows(session, connection.id, ("deposits", [_deposit(1, walletType=7)]))

    assert await keys(session, connection.id) == []
    assert report.new_flags == ["deposits.walletType not known"]


async def test_flags_are_stored_and_notified_once(
    session, run, fake: FakeBinance, connector, clock
) -> None:
    row = _deposit(1, insertTime=NOW_MS - 2 * HOUR_MS, completeTime=NOW_MS - 2 * HOUR_MS)
    fake.add("deposits", {**row, "extraField": "secret-value-4242"})
    connection = await binance_connection(run, config=later_config())
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    clock.ms += 5 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    flag = "deposits.extraField (a field the catalogue does not list)"
    assert after.config["flags"] == [flag]
    assert f"Binance flagged: {flag}" in after.warnings
    [message] = notifier.messages
    assert flag in message and "secret-value" not in message
    assert "secret-value" not in str(after.warnings)
    # The deposit is posted all the same.
    assert await keys(session, connection.id) == [f"deposit:{row['id']}"]
    count = await session.scalar(
        text("select count(*) from finance.binance_deposits where connection_id = :c"),
        {"c": connection.id},
    )
    assert count == 1
