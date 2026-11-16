"""The Monzo normalizer and transfer matching: counterparty fields, own identifiers, and the
contra each transaction is posted with (plan Decisions 9-11)."""

import importlib.util
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier, list_identifiers
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.monzo.normalize import (
    NORMALIZER_VERSION,
    NORMALIZER_VERSION_KEY,
    FetchedRecord,
    NormalizeReport,
    normalize,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import engine as transfer_engine
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
)
from lararium.modules.finance.valuation import balances_asof

from .fixtures import ACCOUNT_ID, make_account, make_balance, make_transaction

KEY = bytes(range(32))
CONNECTOR = Actor("connector", "monzo")
# Synthetic bank details (fixtures.make_account has the Monzo account's own).
MONZO_IDENTIFIER = "998877:87654321"
B_SORT_CODE, B_ACCOUNT_NUMBER = "11-22-33", "12345678"
B_IDENTIFIER = "112233:12345678"
JOINT_ID = "acc_TEST2"
JOINT_SORT_CODE, JOINT_ACCOUNT_NUMBER = "998877", "11223344"
WHEN = "2026-03-01T12:00:00.000Z"
DAY_AFTER = date(2026, 3, 2)


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"normalize made an HTTP request: {request.url}")


async def monzo_connection(run, name: str = "Monzo") -> ConnectionRecord:
    return await run(create_connection, connector="monzo", display_name=name, config={})


async def sync_monzo(
    session: AsyncSession,
    connection: ConnectionRecord,
    *,
    accounts: Sequence[dict[str, Any]] | None = None,
    transactions: Mapping[str, Sequence[dict[str, Any]]] | None = None,
) -> NormalizeReport:
    """Store one sync's raw records (as MonzoConnector.sync would) and normalize them, in a
    command of its own that commits, with no heal after it. `transactions` by Monzo
    account id."""
    accounts = [make_account()] if accounts is None else list(accounts)
    sync_id = uuid.uuid4()
    async with command_context(session, CONNECTOR, "connection.sync", {}) as command:
        fetched: list[FetchedRecord] = []

        async def store(record_type: str, request: dict[str, JSONValue], content: Any) -> None:
            record_id = await store_raw_record(
                command,
                connection_id=connection.id,
                record_type=record_type,
                request=request,
                content=content,
                connector_version="monzo-sync-1",
                sync_id=sync_id,
            )
            fetched.append(FetchedRecord(record_id, request, datetime.now(UTC)))

        await store("accounts", {}, accounts)
        for account in accounts:
            await store("balance", {"account_id": account["id"]}, make_balance())
            await store("pots", {"current_account_id": account["id"]}, [])
        for monzo_id, page in (transactions or {}).items():
            await store("transactions_page", {"account_id": monzo_id}, list(page))
        async with httpx.AsyncClient(transport=httpx.MockTransport(_no_http)) as http:
            ctx = SyncContext(
                command=command,
                connection=await get_connection(session, connection.id),
                sync_id=sync_id,
                master_key=KEY,
                http=http,
                notifier=RecordingNotifier(),
                session_factory=async_sessionmaker(bind=session.bind),
            )
            return await normalize(ctx, fetched)


async def heal(session: AsyncSession) -> int:
    async with command_context(
        session, Actor("system", "transfers"), "finance.heal_transfers", {}
    ) as ctx:
        return await heal_transfers(ctx)


def faster_payment(
    amount: int,
    *,
    tx_id: str = "tx_FPS0001",
    sort_code: str = B_SORT_CODE,
    account_number: str = B_ACCOUNT_NUMBER,
    notes: str = "RENT MARCH",
    **overrides: Any,
) -> dict[str, Any]:
    return (
        make_transaction(
            id=tx_id,
            amount=amount,
            local_amount=amount,
            created=WHEN,
            description="Test Owner",
            category="transfers",
            scheme="payport_faster_payments",
            notes=notes,
            counterparty={
                "name": "Test Owner",
                "sort_code": sort_code,
                "account_number": account_number,
                "user_id": "anonuser_TEST1",
            },
        )
        | overrides
    )


async def manual_bank(run, gbp_id: uuid.UUID, *, identifier: str | None, name: str = "Bank B"):
    """A manual hybrid bank account with 1000.00 on the day before the payment."""
    account = await run(
        create_account, name=name, ledger_type="asset", subtype="bank", balance_mode="hybrid"
    )
    if identifier is not None:
        await run(assign_identifier, account.id, "uk_bank_account", identifier)
    await run(
        record_snapshot,
        account_id=account.id,
        instrument_id=gbp_id,
        quantity=Decimal(1000),
        as_of=datetime(2026, 2, 28, tzinfo=UTC),
    )
    return account


async def account_map(session: AsyncSession, connection: ConnectionRecord) -> dict[str, uuid.UUID]:
    stored = (await get_connection(session, connection.id)).config["account_map"]
    assert isinstance(stored, dict)
    return {k: uuid.UUID(str(v)) for k, v in stored.items()}


async def transaction_row(session: AsyncSession, external_id: str) -> Any:
    return (
        await session.execute(
            text(
                "select id, status, counterparty_identifier_id, counterparty_name,"
                " payment_reference, bank_transfer from finance.transactions"
                " where external_id = :e"
            ),
            {"e": external_id},
        )
    ).one()


async def contra(session: AsyncSession, transaction_id: uuid.UUID, own: uuid.UUID) -> Any:
    return await session.scalar(
        text(
            "select account_id from finance.postings"
            " where transaction_id = :t and account_id <> :own"
        ),
        {"t": transaction_id, "own": own},
    )


async def identifier_row(session: AsyncSession, value: str) -> Any:
    return (
        await session.execute(
            text("select id, account_id from finance.account_identifiers where value = :v"),
            {"v": value},
        )
    ).one_or_none()


async def balances_on(session: AsyncSession, day: date = DAY_AFTER) -> dict[uuid.UUID, Decimal]:
    return {r.account_id: r.quantity for r in await balances_asof(session, day)}


async def count_events(session: AsyncSession, event_type: str) -> int:
    return await session.scalar(
        text("select count(*) from core.events where type = :t"), {"t": event_type}
    )


def test_normalizer_version() -> None:
    assert NORMALIZER_VERSION == "monzo-norm-3"


async def test_faster_payment_to_registered_account(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    bank_b = await manual_bank(run, gbp.id, identifier=None)
    connection = await monzo_connection(run)

    # Bank B isn't registered yet: the payment names an account not known to be the owner's.
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [faster_payment(-10000)]})
    await heal(session)

    row = await transaction_row(session, "tx_FPS0001")
    identifier = await identifier_row(session, B_IDENTIFIER)
    assert identifier is not None
    assert identifier.account_id is None
    assert (
        row.counterparty_identifier_id,
        row.counterparty_name,
        row.payment_reference,
        row.bank_transfer,
    ) == (identifier.id, "Test Owner", "RENT MARCH", True)
    monzo = (await account_map(session, connection))[ACCOUNT_ID]
    unassigned = await run(get_or_create_system_account, "unassigned")
    assert await contra(session, row.id, monzo) == unassigned
    assert await list_transfers(session) == []

    # The owner registers it: the payment is a transfer to Bank B, inferred from Monzo's side.
    await run(assign_identifier, bank_b.id, "uk_bank_account", "112233 12345678")
    await heal(session)

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert (transfer.from_account_id, transfer.to_account_id, transfer.amount) == (
        monzo,
        bank_b.id,
        Decimal("100.00"),
    )
    assert await contra(session, row.id, monzo) == await run(
        get_or_create_system_account, "in_transit"
    )
    assert (await balances_on(session))[bank_b.id] == Decimal("1100.00")
    assert await find_transfer_violations(session) == []


async def test_malformed_counterparty_account_keeps_the_name(session, run) -> None:
    connection = await monzo_connection(run)
    payment = faster_payment(-10000, sort_code="11-22", account_number="1234")

    report = await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})

    assert report.created == 1
    row = await transaction_row(session, "tx_FPS0001")
    assert (row.counterparty_identifier_id, row.counterparty_name, row.bank_transfer) == (
        None,
        "Test Owner",
        True,
    )


async def test_card_payment_and_pot_move_fields(session, run) -> None:
    connection = await monzo_connection(run)
    card = make_transaction(id="tx_CARD", notes="", scheme="mastercard")
    pot_move = make_transaction(
        id="tx_POT", scheme="uk_retail_pot", metadata={"pot_id": "pot_GONE"}, notes="x"
    )

    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [card, pot_move]})

    for external_id in ("tx_CARD", "tx_POT"):
        row = await transaction_row(session, external_id)
        assert (
            row.counterparty_identifier_id,
            row.counterparty_name,
            row.payment_reference,
            row.bank_transfer,
        ) == (None, None, None, False)


async def test_monzo_registers_its_own_identifiers(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    # The owner registered the main account's details on another account before syncing.
    elsewhere = await manual_bank(run, gbp.id, identifier=MONZO_IDENTIFIER, name="Old record")
    connection = await monzo_connection(run)
    joint = make_account(
        id=JOINT_ID,
        description="uk_retail_joint",
        type="uk_retail_joint",
        sort_code=JOINT_SORT_CODE,
        account_number=JOINT_ACCOUNT_NUMBER,
    )

    await sync_monzo(session, connection, accounts=[make_account(), joint])
    await sync_monzo(session, connection, accounts=[make_account(), joint])

    mapped = await account_map(session, connection)
    by_value = {i.value: i.account_id for i in await list_identifiers(session)}
    assert by_value == {
        MONZO_IDENTIFIER: elsewhere.id,  # the owner's registration wins
        "998877:11223344": mapped[JOINT_ID],
    }
    assigned = await count_events(session, "finance.account_identifier.assigned")
    assert assigned == 2  # the owner's, and the joint account's once


async def test_resync_after_match_writes_nothing(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    payments = {ACCOUNT_ID: [faster_payment(-10000)]}
    await sync_monzo(session, connection, transactions=payments)
    await heal(session)
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    updated = await count_events(session, "finance.transaction.updated")

    report = await sync_monzo(session, connection, transactions=payments)

    assert (report.created, report.updated, report.unchanged) == (0, 0, 1)
    assert await count_events(session, "finance.transaction.updated") == updated
    assert await heal(session) == 0
    assert [t.id for t in await list_transfers(session)] == [transfer.id]


async def test_declined_pending_removes_placeholder(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    bank_b = await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    pending = faster_payment(-10000, settled="")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [pending]})
    await heal(session)
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    assert (await balances_on(session))[bank_b.id] == Decimal("1100.00")

    declined = pending | {"decline_reason": "INSUFFICIENT_FUNDS"}
    report = await sync_monzo(session, connection, transactions={ACCOUNT_ID: [declined]})
    await heal(session)

    assert report.voided == 1
    assert await list_transfers(session) == []
    placeholders = await session.scalar(
        text("select count(*) from finance.transactions where origin = 'inference'")
    )
    assert placeholders == 0
    assert (await balances_on(session))[bank_b.id] == Decimal("1000.00")
    assert await find_transfer_violations(session) == []


async def _clear_fields(session: AsyncSession) -> None:
    await session.execute(
        text(
            "update finance.transactions set counterparty_identifier_id = null,"
            " counterparty_name = null, payment_reference = null, bank_transfer = false"
            " where origin = 'connector'"
        )
    )
    await session.commit()


async def test_existing_history_gets_fields_once(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    older = faster_payment(-10000, notes="OLD REF")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [older]})
    assert (await get_connection(session, connection.id)).config["counterparty_fields_v1"] is True
    # As the M3 normalizer left it: no counterparty fields, and no flag.
    await _clear_fields(session)
    await run(update_config, connection.id, {}, remove=["counterparty_fields_v1"])
    # A newer raw record of the same transaction wins over the older one.
    newer = older | {"notes": "NEW REF"}
    sync_id = uuid.uuid4()
    async with command_context(session, CONNECTOR, "connection.sync", {}) as command:
        await store_raw_record(
            command,
            connection_id=connection.id,
            record_type="transactions_page",
            request={"account_id": ACCOUNT_ID},
            content=[newer],
            connector_version="monzo-sync-1",
            sync_id=sync_id,
        )

    # The next sync fetches nothing new for it.
    await sync_monzo(session, connection)

    row = await transaction_row(session, "tx_FPS0001")
    identifier = await identifier_row(session, B_IDENTIFIER)
    assert (
        row.counterparty_identifier_id,
        row.counterparty_name,
        row.payment_reference,
        row.bank_transfer,
    ) == (identifier.id, "Test Owner", "NEW REF", True)
    assert (await get_connection(session, connection.id)).config["counterparty_fields_v1"] is True
    # Identified now, so the heal after the sync finds the transfer.
    await heal(session)
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"

    # Once only: the flag stops a second pass.
    await _clear_fields(session)
    await sync_monzo(session, connection)
    assert (await transaction_row(session, "tx_FPS0001")).counterparty_name is None


async def test_payment_with_fees_stays_on_unassigned(session, run, monkeypatch) -> None:
    # Monzo's fees are postings of their own on the account, besides its posting and the
    # contra: such a payment can't be a transfer's leg (LEG_POSTINGS), even to an own account.
    gbp = await get_currency(session, "GBP")
    await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    payment = faster_payment(-10100, fees={"test.fee": -100})

    report = await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})

    assert report.created == 1
    row = await transaction_row(session, "tx_FPS0001")
    monzo = (await account_map(session, connection))[ACCOUNT_ID]
    postings = (
        await session.execute(
            text(
                "select account_id, quantity, is_fee from finance.postings"
                " where transaction_id = :t order by position"
            ),
            {"t": row.id},
        )
    ).all()
    unassigned = await run(get_or_create_system_account, "unassigned")
    assert [tuple(p) for p in postings] == [
        (monzo, Decimal("-100.00"), False),
        (unassigned, Decimal("101.00"), False),
        (monzo, Decimal("-1.00"), True),
    ]
    assert row.counterparty_identifier_id == (await identifier_row(session, B_IDENTIFIER)).id

    repointed: list[Any] = []
    real_repoint = transfer_engine.repoint_contra

    async def spy(*args: Any, **kwargs: Any) -> bool:
        repointed.append(args)
        return await real_repoint(*args, **kwargs)

    monkeypatch.setattr(transfer_engine, "repoint_contra", spy)
    await heal(session)
    assert repointed == []
    assert await list_transfers(session) == []

    report = await sync_monzo(session, connection, transactions={ACCOUNT_ID: [payment]})
    assert (report.created, report.updated, report.unchanged) == (0, 0, 1)
    assert await contra(session, row.id, monzo) == unassigned
    assert await find_transfer_violations(session) == []


async def test_replay_and_counterparty_backfill_agree(session, run) -> None:
    # A connection the structured-fields normalizer last ran (an older NORMALIZER_VERSION)
    # with transactions posted before counterparty fields existed: its next sync runs both
    # the counterparty backfill and the version replay. They read the same latest copy, so
    # the replay finds the backfill's fields and leaves the transfer's In transit leg alone.
    gbp = await get_currency(session, "GBP")
    await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    first = faster_payment(-10000, notes="NEW REF", updated="2026-03-01T12:00:00.000Z")
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [first]})
    await heal(session)
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"
    row = await transaction_row(session, "tx_FPS0001")
    monzo = (await account_map(session, connection))[ACCOUNT_ID]
    in_transit = await run(get_or_create_system_account, "in_transit")
    assert await contra(session, row.id, monzo) == in_transit

    await _clear_fields(session)
    await run(
        update_config,
        connection.id,
        {NORMALIZER_VERSION_KEY: "monzo-norm-2"},
        remove=["counterparty_fields_v1"],
    )
    # A copy fetched later that Monzo updated earlier: the latest is by Monzo's `updated`,
    # for the backfill as for the replay, so both read the posted copy, not this one.
    stale = first | {"notes": "STALE REF", "updated": "2026-02-28T12:00:00.000Z"}
    async with command_context(session, CONNECTOR, "connection.sync", {}) as command:
        await store_raw_record(
            command,
            connection_id=connection.id,
            record_type="transactions_page",
            request={"account_id": ACCOUNT_ID},
            content=[stale],
            connector_version="monzo-sync-1",
            sync_id=uuid.uuid4(),
        )
    updates = await count_events(session, "finance.transaction.updated")

    report = await sync_monzo(session, connection)

    row = await transaction_row(session, "tx_FPS0001")
    identifier = await identifier_row(session, B_IDENTIFIER)
    assert (
        row.counterparty_identifier_id,
        row.counterparty_name,
        row.payment_reference,
        row.bank_transfer,
    ) == (identifier.id, "Test Owner", "NEW REF", True)
    # The backfill wrote the fields; the replay found them, and the transaction, unchanged.
    assert await count_events(session, "finance.transaction.updated") == updates + 1
    assert (report.updated, report.unchanged) == (0, 1)
    assert await contra(session, row.id, monzo) == in_transit
    config = (await get_connection(session, connection.id)).config
    assert (config["counterparty_fields_v1"], config[NORMALIZER_VERSION_KEY]) == (
        True,
        NORMALIZER_VERSION,
    )
    await heal(session)
    [after] = await list_transfers(session)
    assert (after.id, after.status) == (transfer.id, "inferred")
    assert await find_transfer_violations(session) == []


def _migration(name: str) -> Any:
    path = Path(__file__).parents[3] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reupgrading_0015_fills_the_fields_again(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    await run(update_config, connection.id, {"other": "kept"})
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [faster_payment(-10000)]})
    assert (await get_connection(session, connection.id)).config["counterparty_fields_v1"] is True

    # A downgrade and re-upgrade of 0015: the columns come back empty, and both ends of it
    # forget the note that they were filled.
    migration = _migration("0015_finance_transfers")
    assert migration.FORGET_COUNTERPARTY_BACKFILL in migration.UPGRADE
    assert migration.FORGET_COUNTERPARTY_BACKFILL in migration.LEDGER_UNDO
    await _clear_fields(session)
    await session.execute(text(migration.FORGET_COUNTERPARTY_BACKFILL))
    await session.commit()
    config = (await get_connection(session, connection.id)).config
    assert "counterparty_fields_v1" not in config
    assert config["other"] == "kept"

    # The next sync, with nothing new fetched, fills them again from the raw records.
    await sync_monzo(session, connection)

    row = await transaction_row(session, "tx_FPS0001")
    identifier = await identifier_row(session, B_IDENTIFIER)
    assert (
        row.counterparty_identifier_id,
        row.counterparty_name,
        row.payment_reference,
        row.bank_transfer,
    ) == (identifier.id, "Test Owner", "RENT MARCH", True)
    assert (await get_connection(session, connection.id)).config["counterparty_fields_v1"] is True
    await heal(session)
    [transfer] = await list_transfers(session)
    assert transfer.status == "inferred"

    # A config without the note is left as it is.
    before = (await get_connection(session, connection.id)).config
    await run(update_config, connection.id, {}, remove=["counterparty_fields_v1"])
    await session.execute(text(migration.FORGET_COUNTERPARTY_BACKFILL))
    after = (await get_connection(session, connection.id)).config
    assert after == {k: v for k, v in before.items() if k != "counterparty_fields_v1"}


async def test_two_monzo_connections_match(session, run) -> None:
    personal = await monzo_connection(run, "Monzo")
    joint_connection = await monzo_connection(run, "Monzo Joint")
    joint = make_account(
        id=JOINT_ID,
        description="uk_retail_joint",
        type="uk_retail_joint",
        sort_code=JOINT_SORT_CODE,
        account_number=JOINT_ACCOUNT_NUMBER,
    )
    sent = faster_payment(
        -10000,
        tx_id="tx_OUT",
        sort_code=JOINT_SORT_CODE,
        account_number=JOINT_ACCOUNT_NUMBER,
        scheme="monzo_to_monzo",
    )
    received = faster_payment(
        10000,
        tx_id="tx_IN",
        sort_code="99-88-77",
        account_number="87654321",
        account_id=JOINT_ID,
        created="2026-03-01T12:00:05.000Z",
        scheme="monzo_to_monzo",
    )

    await sync_monzo(session, personal, transactions={ACCOUNT_ID: [sent]})
    await heal(session)
    await sync_monzo(
        session, joint_connection, accounts=[joint], transactions={JOINT_ID: [received]}
    )
    await heal(session)

    personal_id = (await account_map(session, personal))[ACCOUNT_ID]
    joint_id = (await account_map(session, joint_connection))[JOINT_ID]
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.from_account_id, transfer.to_account_id) == (
        "matched",
        personal_id,
        joint_id,
    )
    in_transit = await run(get_or_create_system_account, "in_transit")
    out_row, in_row = (
        await transaction_row(session, "tx_OUT"),
        await transaction_row(session, "tx_IN"),
    )
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (out_row.id, in_row.id)
    assert await contra(session, out_row.id, personal_id) == in_transit
    assert await contra(session, in_row.id, joint_id) == in_transit
    assert (await balances_on(session)).get(in_transit, Decimal(0)) == 0
    # Both re-synced: nothing changes.
    assert (await sync_monzo(session, personal, transactions={ACCOUNT_ID: [sent]})).updated == 0
    report = await sync_monzo(
        session, joint_connection, accounts=[joint], transactions={JOINT_ID: [received]}
    )
    assert report.updated == 0
    assert await heal(session) == 0
    assert await find_transfer_violations(session) == []
