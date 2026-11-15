"""The worker and transfers: the heal after each sync, and the periodic check that heals and
tells the owner about overdue transfers and those needing review."""

import base64
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection, get_connection, set_status
from lararium.core.connectors import Connector, SyncContext
from lararium.core.json_types import JSONValue
from lararium.db import create_engine, make_sessionmaker
from lararium.modules import registry
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    list_transfers,
    match_transaction,
    transfer_contra,
)
from lararium.modules.finance.valuation import net_worth_series
from lararium.worker import jobs

from ..finance.monzo.fixtures import ACCOUNT_ID
from ..finance.monzo.test_normalize_transfers import (
    B_IDENTIFIER,
    RecordingNotifier,
    account_map,
    faster_payment,
    manual_bank,
    monzo_connection,
    sync_monzo,
)

SETTINGS = Settings(
    database_url="postgresql+psycopg://x@localhost/x",
    auth_mode="dev",
    master_key=SecretStr(base64.b64encode(bytes(range(32))).decode()),
)
BROKER_IDENTIFIER = "445566:11112222"
BANK_A_IDENTIFIER = "778899:33334444"


async def _net_worth(session, day: date) -> Decimal:
    [point] = await net_worth_series(session, day, day)
    return point.value


async def test_heal_repairs_after_lost_matching(session, run) -> None:
    gbp = await get_currency(session, "GBP")
    bank_b = await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    connection = await monzo_connection(run)
    day = date(2026, 3, 2)
    # The sync commits, and the worker dies before its heal.
    await sync_monzo(session, connection)
    worth = await _net_worth(session, day)
    await sync_monzo(session, connection, transactions={ACCOUNT_ID: [faster_payment(-10000)]})

    # Already right meanwhile: the payment sits in In transit, not spent.
    assert await list_transfers(session) == []
    assert await _net_worth(session, day) == worth

    notifier = RecordingNotifier()
    await jobs.run_check_transfers(session, notifier=notifier)

    [transfer] = await list_transfers(session)
    monzo = (await account_map(session, connection))[ACCOUNT_ID]
    assert (transfer.status, transfer.from_account_id, transfer.to_account_id) == (
        "inferred",
        monzo,
        bank_b.id,
    )
    assert await _net_worth(session, day) == worth
    assert notifier.messages == []


class _PostingConnector:
    """Posts one identified payment from its account, as a normalizer would."""

    name = "fake"
    version = "1"
    capabilities = frozenset[str]()

    def __init__(self, account_id, identifier_id) -> None:
        self.account_id = account_id
        self.identifier_id = identifier_id

    async def sync(self, ctx: SyncContext) -> JSONValue:
        session = ctx.command.session
        gbp = await get_currency(session, "GBP")
        # Shared rows in a transaction of their own, as the Monzo normalizer creates them.
        async with (
            ctx.session_factory() as own,
            command_context(own, ctx.command.actor, "finance.shared_row", {}) as shared,
        ):
            await get_or_create_system_account(shared, "unassigned")
            await get_or_create_system_account(shared, "in_transit")
        contra = await transfer_contra(
            session,
            source=None,
            own_account_id=self.account_id,
            counterparty_identifier_id=self.identifier_id,
        )
        await upsert_connector_transaction(
            ctx.command,
            TransactionInput(
                occurred_at=datetime(2026, 3, 1, 12, tzinfo=UTC),
                description="Transfer",
                origin="connector",
                connection_id=ctx.connection.id,
                external_id="tx_1",
                postings=[
                    PostingInput(self.account_id, gbp.id, Decimal(-100)),
                    PostingInput(contra, gbp.id, Decimal(100)),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=self.identifier_id, bank_transfer=True
                ),
            ),
        )
        return {"since": "t1"}


async def _connected_with_payment(session, run, monkeypatch) -> tuple:
    gbp = await get_currency(session, "GBP")
    bank_b = await manual_bank(run, gbp.id, identifier=B_IDENTIFIER)
    identifier_id = await session.scalar(
        text("select id from finance.account_identifiers where value = :v"), {"v": B_IDENTIFIER}
    )
    connection = await run(create_connection, connector="fake", display_name="Bank", config={})
    await run(set_status, connection.id, "healthy")
    account = await run(
        create_account,
        name="Bank",
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=connection.id,
    )
    connector = _PostingConnector(account.id, identifier_id)

    def lookup(name: str) -> Connector:
        assert name == "fake"
        return connector  # pyright: ignore[reportReturnType]

    monkeypatch.setattr(registry, "connector", lookup)
    return connection, bank_b


async def _run_sync(session, connection, notifier) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    ) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=SETTINGS,
            http=http,
            notifier=notifier,
            session_factory=async_sessionmaker(bind=session.bind),
        )


async def test_sync_heals_after_it_commits(session, run, monkeypatch) -> None:
    connection, bank_b = await _connected_with_payment(session, run, monkeypatch)

    await _run_sync(session, connection, RecordingNotifier())

    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.to_account_id) == ("inferred", bank_b.id)
    heals = await session.scalar(
        text(
            "select count(*) from core.audit_log"
            " where actor_type = 'system' and actor_id = 'transfers'"
            " and command = 'finance.heal_transfers' and args->>'connection_id' = :id"
        ),
        {"id": str(connection.id)},
    )
    assert heals == 1


async def test_heal_failure_does_not_fail_sync(session, run, monkeypatch, caplog) -> None:
    connection, _ = await _connected_with_payment(session, run, monkeypatch)
    calls: list[str] = []

    async def broken_heal(ctx) -> int:
        calls.append(ctx.actor.id)
        raise RuntimeError("heal broke")

    monkeypatch.setattr(jobs, "heal_transfers", broken_heal)
    notifier = RecordingNotifier()

    with caplog.at_level(logging.ERROR, logger="lararium.worker.jobs"):
        await _run_sync(session, connection, notifier)

    assert calls == ["transfers"]
    after = await get_connection(session, connection.id)
    assert (after.status, after.cursor, after.consecutive_failures) == (
        "healthy",
        {"since": "t1"},
        0,
    )
    # The sync's own work is committed; only the matching is left to the next heal.
    posted = await session.scalar(
        text("select count(*) from finance.transactions where external_id = 'tx_1'")
    )
    assert posted == 1
    assert await list_transfers(session) == []
    assert notifier.messages == []
    assert "RuntimeError" in caplog.text
    assert "heal broke" not in caplog.text


async def _connected_account(run, name: str, identifier: str):
    connection = await run(create_connection, connector="monzo", display_name=name, config={})
    account = await run(
        create_account,
        name=name,
        ledger_type="asset",
        subtype="bank",
        balance_mode="transactions",
        source_connection_id=connection.id,
    )
    await run(assign_identifier, account.id, "uk_bank_account", identifier)
    return connection, account


async def _post(session, run, connection, account, quantity: str, *, external_id, when, names):
    gbp = await get_currency(session, "GBP")
    await run(get_or_create_system_account, "unassigned")
    await run(get_or_create_system_account, "in_transit")
    identifier_id = (
        None
        if names is None
        else await session.scalar(
            text("select id from finance.account_identifiers where value = :v"), {"v": names}
        )
    )
    contra = await transfer_contra(
        session,
        source=("connection", f"{connection.id}:{external_id}"),
        own_account_id=account.id,
        counterparty_identifier_id=identifier_id,
    )
    amount = Decimal(quantity)
    await run(
        upsert_connector_transaction,
        TransactionInput(
            occurred_at=when,
            description="Transfer",
            origin="connector",
            connection_id=connection.id,
            external_id=external_id,
            postings=[
                PostingInput(account.id, gbp.id, amount),
                PostingInput(contra, gbp.id, -amount),
            ],
            counterparty=CounterpartyFields(identifier_id=identifier_id, bank_transfer=True),
        ),
    )


async def test_overdue_and_review_notified_once(session, run) -> None:
    now = datetime.now(UTC)
    monzo_connection_, monzo = await _connected_account(run, "Monzo", "998877:87654321")
    broker_connection, broker = await _connected_account(run, "Broker", BROKER_IDENTIFIER)
    bank_a_connection, bank_a = await _connected_account(run, "Bank A", BANK_A_IDENTIFIER)
    # The broker's history starts before the payment, so it is awaited, not before_history.
    await _post(
        session,
        run,
        broker_connection,
        broker,
        "-5",
        external_id="card_1",
        when=now - timedelta(days=20),
        names=None,
    )
    # Sent 8 days ago and not arrived: overdue.
    await _post(
        session,
        run,
        monzo_connection_,
        monzo,
        "-100",
        external_id="out_broker",
        when=now - timedelta(days=8),
        names=BROKER_IDENTIFIER,
    )
    # Sent to Bank A, which has two unidentified arrivals of that amount: needs review.
    await _post(
        session,
        run,
        monzo_connection_,
        monzo,
        "-100",
        external_id="out_bank_a",
        when=now - timedelta(days=1),
        names=BANK_A_IDENTIFIER,
    )
    for n in (1, 2):
        await _post(
            session,
            run,
            bank_a_connection,
            bank_a,
            "100",
            external_id=f"in_{n}",
            when=now - timedelta(days=1) + timedelta(hours=n),
            names=None,
        )
    notifier = RecordingNotifier()

    await jobs.run_check_transfers(session, notifier=notifier)

    assert sorted(notifier.messages) == [
        "A transfer of 100.00 GBP from Monzo to Bank A needs your review.",
        "A transfer of 100.00 GBP from Monzo to Broker has been in transit for 8 days.",
    ]
    before = {t.id for t in await list_transfers(session)}
    assert len(before) == 2

    # Matching both again reopens them under new ids (as a request that changes a decision
    # does); the heal and check after it send nothing new.
    async with command_context(session, Actor("user", "test"), "rematch", {}) as ctx:
        for external_id in ("out_broker", "out_bank_a"):
            transaction_id = await session.scalar(
                text("select id from finance.transactions where external_id = :e"),
                {"e": external_id},
            )
            await match_transaction(ctx, transaction_id)
    after = await list_transfers(session)
    assert {t.id for t in after}.isdisjoint(before)
    assert all(t.notified_at is not None for t in after)
    await jobs.run_check_transfers(session, notifier=notifier)

    assert len(notifier.messages) == 2


async def _overdue_to_broker(session, run) -> None:
    """Monzo → Broker, sent 8 days ago and awaiting: overdue."""
    now = datetime.now(UTC)
    monzo_connection_, monzo = await _connected_account(run, "Monzo", "998877:87654321")
    broker_connection, broker = await _connected_account(run, "Broker", BROKER_IDENTIFIER)
    await _post(
        session,
        run,
        broker_connection,
        broker,
        "-5",
        external_id="card_1",
        when=now - timedelta(days=20),
        names=None,
    )
    await _post(
        session,
        run,
        monzo_connection_,
        monzo,
        "-100",
        external_id="out_broker",
        when=now - timedelta(days=8),
        names=BROKER_IDENTIFIER,
    )
    # Opened by an earlier heal.
    async with command_context(session, Actor("user", "test"), "match", {}) as ctx:
        transaction_id = await session.scalar(
            text("select id from finance.transactions where external_id = 'out_broker'")
        )
        await match_transaction(ctx, transaction_id)


async def test_check_transfers_still_notifies_when_heal_fails(session, run, monkeypatch) -> None:
    async def broken_heal(ctx) -> int:
        raise RuntimeError("heal broke")

    monkeypatch.setattr(jobs, "heal_transfers", broken_heal)
    await _overdue_to_broker(session, run)
    notifier = RecordingNotifier()

    await jobs.run_check_transfers(session, notifier=notifier)

    assert notifier.messages == [
        "A transfer of 100.00 GBP from Monzo to Broker has been in transit for 8 days."
    ]


async def test_check_transfers_notifier_failure_does_not_fail(session, run) -> None:
    class BrokenNotifier:
        def __init__(self) -> None:
            self.attempts = 0

        async def send(self, text: str) -> None:
            self.attempts += 1
            raise RuntimeError("feishu is down")

    await _overdue_to_broker(session, run)
    notifier = BrokenNotifier()

    await jobs.run_check_transfers(session, notifier=notifier)
    await jobs.run_check_transfers(session, notifier=notifier)

    # Marked as told even though the send failed: a message lost to an outage is not
    # retried, so a flaky channel can't repeat it either.
    assert notifier.attempts == 1
    [transfer] = await list_transfers(session)
    assert transfer.notified_at is not None


async def test_matching_lock_is_free_while_sending(
    backend_url: str, owner_url: str, migrated: None, monkeypatch
) -> None:
    # Real commits through their own engine: a fixture session's outer transaction would
    # hold the lock to the end of the test whatever the job does.
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    lock_free: list[bool] = []

    async def due(session, now) -> list[tuple[uuid.UUID, str]]:
        return [(uuid.uuid4(), "A transfer needs your review.")]

    class ProbingNotifier:
        async def send(self, message: str) -> None:
            # What a heal elsewhere would find while the message is on its way.
            async with sessions() as other, other.begin():
                lock_free.append(
                    await other.scalar(
                        text("select pg_try_advisory_xact_lock(hashtext('finance.transfers'))")
                    )
                )

    monkeypatch.setattr(jobs, "due_notifications", due)
    try:
        async with sessions() as session:
            started = await session.scalar(text("select clock_timestamp()"))
            await session.rollback()
            await jobs.run_check_transfers(session, notifier=ProbingNotifier())
        assert lock_free == [True]
    finally:
        owner = create_engine(owner_url)
        try:
            # The audit log is append-only for the backend role.
            async with owner.begin() as conn:
                await conn.execute(
                    text(
                        "delete from core.audit_log where actor_type = 'system'"
                        " and actor_id = 'transfers' and occurred_at >= :started"
                    ),
                    {"started": started},
                )
        finally:
            await owner.dispose()
            await engine.dispose()


async def test_many_due_notifications_go_as_one_summary(session, monkeypatch) -> None:
    # The first check after deploy may find the whole history due at once.
    due = [(uuid.uuid4(), f"A transfer {n} needs your review.") for n in range(4)]
    marked: list[uuid.UUID] = []

    async def due_notifications(session, now) -> list[tuple[uuid.UUID, str]]:
        return due

    async def mark_notified(ctx, transfer_ids) -> None:
        marked.extend(transfer_ids)

    monkeypatch.setattr(jobs, "due_notifications", due_notifications)
    monkeypatch.setattr(jobs, "mark_notified", mark_notified)
    notifier = RecordingNotifier()

    await jobs.run_check_transfers(session, notifier=notifier)

    assert notifier.messages == ["4 transfers need your attention: see Transfers"]
    assert marked == [transfer_id for transfer_id, _ in due]

    # Up to 3 still go one by one.
    del due[3]
    notifier = RecordingNotifier()
    await jobs.run_check_transfers(session, notifier=notifier)
    assert notifier.messages == [text for _, text in due]
