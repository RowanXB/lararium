from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.core.documents import record_document
from lararium.core.entities import get_self_entity_id
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, create_account
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.importing.profiles import QifProfileConfig, create_profile
from lararium.modules.finance.instruments import InstrumentRecord
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    TransactionRecord,
    TransactionStatus,
    find_unbalanced_transactions,
    post_transaction,
    upsert_connector_transaction,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    transfer_contra,
)
from lararium.modules.finance.valuation import balances_asof, net_worth_series

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)
DAY = date(2026, 11, 3)  # the day after WHEN
B_IDENTIFIER = "112233:12345678"

type Run = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class SystemIds:
    unassigned: UUID
    in_transit: UUID
    opening_balances: UUID


@pytest.fixture
async def ids(run: Run) -> SystemIds:
    return SystemIds(
        unassigned=await run(get_or_create_system_account, "unassigned"),
        in_transit=await run(get_or_create_system_account, "in_transit"),
        opening_balances=await run(get_or_create_system_account, "opening_balances"),
    )


@dataclass(frozen=True)
class Connected:
    connection_id: UUID
    account: AccountRecord


type MakeConnected = Callable[..., Awaitable[Connected]]


@pytest.fixture
def connected_bank(run: Run) -> MakeConnected:
    """A bank account with a connection of its own, valued from its transactions."""

    async def _make(name: str = "A") -> Connected:
        connection = await run(create_connection, connector="monzo", display_name=name, config={})
        account = await run(
            create_account,
            name=name,
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
            source_connection_id=connection.id,
        )
        return Connected(connection.id, account)

    return _make


type MakeManual = Callable[..., Awaitable[AccountRecord]]


@pytest.fixture
def manual_bank(run: Run, gbp: InstrumentRecord) -> MakeManual:
    """A manual hybrid bank account, optionally registered under an identifier and with an
    opening snapshot (a day before WHEN by default)."""

    async def _make(
        name: str = "B",
        *,
        identifier: str | None = B_IDENTIFIER,
        snapshot: Decimal | None = Decimal("1000"),
        snapshot_at: datetime = WHEN - timedelta(days=1),
    ) -> AccountRecord:
        account = await run(
            create_account, name=name, ledger_type="asset", subtype="bank", balance_mode="hybrid"
        )
        if identifier is not None:
            await run(assign_identifier, account.id, "uk_bank_account", identifier)
        if snapshot is not None:
            await run(
                record_snapshot,
                account_id=account.id,
                instrument_id=gbp.id,
                quantity=snapshot,
                as_of=snapshot_at,
            )
        return account

    return _make


type ConnectorTx = Callable[..., Awaitable[TransactionRecord]]


@pytest.fixture
def connector_tx(
    session: AsyncSession, run: Run, gbp: InstrumentRecord, ids: SystemIds
) -> ConnectorTx:
    """Post (or re-post) a connector transaction as a normalizer would: its contra from
    transfer_contra, and the given counterparty fields."""

    async def _post(
        bank: Connected,
        quantity: str,
        *,
        external_id: str = "tx_1",
        occurred_at: datetime = WHEN,
        identifier_id: UUID | None = None,
        bank_transfer: bool = True,
        status: TransactionStatus = "booked",
        description: str = "Transfer",
    ) -> TransactionRecord:
        contra = await transfer_contra(
            session,
            source=("connection", f"{bank.connection_id}:{external_id}"),
            own_account_id=bank.account.id,
            counterparty_identifier_id=identifier_id,
            other_postings=0,
        )
        amount = Decimal(quantity)
        record, _ = await run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=occurred_at,
                description=description,
                status=status,
                origin="connector",
                connection_id=bank.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(bank.account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=identifier_id, name="Test Owner", bank_transfer=bank_transfer
                ),
            ),
        )
        return record

    return _post


type Heal = Callable[[], Awaitable[int]]


@pytest.fixture
def heal(session: AsyncSession) -> Heal:
    """heal_transfers in a command of its own, as the worker runs it."""

    async def _heal() -> int:
        async with command_context(
            session, Actor("system", "transfers"), "finance.heal_transfers", {}
        ) as ctx:
            return await heal_transfers(ctx)

    return _heal


async def balances_on(session: AsyncSession, day: date = DAY) -> dict[UUID, Decimal]:
    return {r.account_id: r.quantity for r in await balances_asof(session, day)}


async def net_worth_on(session: AsyncSession, day: date = DAY) -> Decimal:
    [point] = await net_worth_series(session, day, day)
    return point.value


async def contra_account(session: AsyncSession, transaction_id: UUID, own_account_id: UUID) -> Any:
    return await session.scalar(
        text(
            "select account_id from finance.postings"
            " where transaction_id = :t and account_id <> :own"
        ),
        {"t": transaction_id, "own": own_account_id},
    )


async def placeholders(session: AsyncSession, origin: str = "inference") -> list[Any]:
    """The derived transactions of one origin: placeholders, or settlements ("rule")."""
    rows = await session.execute(
        text(
            "select t.id, t.description, t.status, t.occurred_at, t.booking_date"
            " from finance.transactions t where t.origin = :origin order by t.occurred_at"
        ),
        {"origin": origin},
    )
    return list(rows)


async def postings_of(session: AsyncSession, transaction_id: UUID) -> set[tuple[UUID, Decimal]]:
    rows = await session.execute(
        text("select account_id, quantity from finance.postings where transaction_id = :t"),
        {"t": transaction_id},
    )
    return {(r.account_id, r.quantity) for r in rows}


async def assert_consistent(session: AsyncSession) -> None:
    assert await find_transfer_violations(session) == []
    assert await find_unbalanced_transactions(session) == []


A_IDENTIFIER = "445566:11112222"
C_IDENTIFIER = "778899:33334444"


@pytest.fixture
def registered(run: Run, connected_bank: MakeConnected):
    """A connected bank account with its own identifier registered to it."""

    async def _make(name: str, value: str) -> tuple[Connected, UUID]:
        bank = await connected_bank(name)
        identifier = await run(assign_identifier, bank.account.id, "uk_bank_account", value)
        return bank, identifier.id

    return _make


@pytest.fixture
async def a_and_c(registered: Any) -> tuple[Connected, UUID, Connected, UUID]:
    """A and C, each on a connection of its own and each registered."""
    a, a_id = await registered("A", A_IDENTIFIER)
    c, c_id = await registered("C", C_IDENTIFIER)
    return a, a_id, c, c_id


async def history(connector_tx: ConnectorTx, bank: Connected, days_before: int = 10) -> Any:
    """A card payment on the account's connection, so its history starts days_before WHEN."""
    return await connector_tx(
        bank,
        "-5",
        external_id="card_1",
        occurred_at=WHEN - timedelta(days=days_before),
        bank_transfer=False,
        description="Coffee",
    )


async def in_transit_balance(session: AsyncSession, ids: SystemIds) -> Decimal:
    return (await balances_on(session)).get(ids.in_transit, Decimal(0))


type RecordImport = Callable[..., Awaitable[None]]
# What an import lists in full by default: well around WHEN, from the manual accounts'
# default snapshot day on.
IMPORTED = (WHEN.date() - timedelta(days=1), WHEN.date() + timedelta(days=60))


@dataclass(frozen=True)
class RowDetails:
    """What an import row's file prints of its counterparty's bank account."""

    sort_code: str | None = None
    account_number: str | None = None
    account_ending: str | None = None


async def insert_batch(
    session: AsyncSession,
    run: Run,
    account_id: UUID,
    complete: tuple[date, date],
    status: Literal["committed", "rolled_back"] = "committed",
) -> UUID:
    """A batch of the account's with the given status, listing `complete` in full."""
    config = QifProfileConfig(format="qif", date_format="%d/%m/%Y")
    profile = await run(create_profile, name=f"Statements {uuid4()}", config=config)
    document = await run(
        record_document,
        uuid4().bytes,
        filename="statement.qif",
        media_type="application/qif",
        source="finance.import",
    )
    b = models.ImportBatch
    committed = status == "committed"
    batch_id = await session.scalar(
        insert(b)
        .values(
            account_id=account_id,
            profile_id=profile.id,
            profile_version=1,
            profile_config=config.model_dump(mode="json"),
            parser_version="test",
            document_id=document.id,
            filename="statement.qif",
            status=status,
            committed_at=func.clock_timestamp(),
            rolled_back_at=None if committed else func.clock_timestamp(),
            covered_from=complete[0],
            covered_to=complete[1],
            complete_from=complete[0],
            complete_to=complete[1],
            owner_entity_id=await get_self_entity_id(session),
        )
        .returning(b.id)
    )
    assert batch_id is not None
    return batch_id


async def record_import_rows(
    session: AsyncSession,
    run: Run,
    account_id: UUID,
    transaction_ids: list[UUID],
    complete: tuple[date, date] = IMPORTED,
    *,
    keys: list[str] | None = None,
    details: Sequence[RowDetails] | None = None,
    time_unit: str = "day",
) -> None:
    """Record posted transactions as the rows of a committed statement import (M2) of their
    account, listing `complete` in full. Matching takes a counterpart found by amount and
    date only on days imports list in full (design §5.8), so a row posted as an import
    is recorded as one. `keys`: the rows' dedupe keys, which a row imported again keeps
    (its reference, source kind "import"); by default one per transaction. `details`: what
    each row prints of its counterparty's bank account; by default nothing. `time_unit`:
    what the file gave the rows' times to; by default a date only, as a QIF gives."""
    r, t, p = models.ImportRow, models.Transaction, models.Posting
    batch_id = await insert_batch(session, run, account_id, complete)
    if keys is None:
        keys = [f"test:{t}" for t in transaction_ids]
    if details is None:
        details = [RowDetails()] * len(transaction_ids)
    for position, (transaction_id, key, printed) in enumerate(
        zip(transaction_ids, keys, details, strict=True)
    ):
        posted = (
            await session.execute(
                select(t.booking_date, t.occurred_at, t.description, p.instrument_id, p.quantity)
                .join(p, p.transaction_id == t.id)
                .where(t.id == transaction_id, p.account_id == account_id)
            )
        ).one()
        await session.execute(
            insert(r).values(
                batch_id=batch_id,
                account_id=account_id,
                position=position,
                kind="statement",
                status="committed",
                booking_date=posted.booking_date,
                occurred_at=posted.occurred_at,
                time_unit=time_unit,
                description=posted.description,
                quantity=posted.quantity,
                instrument_id=posted.instrument_id,
                counterparty_sort_code=printed.sort_code,
                counterparty_account_number=printed.account_number,
                counterparty_account_ending=printed.account_ending,
                dedupe_key=key,
                raw={},
                transaction_id=transaction_id,
            )
        )


@pytest.fixture
def record_import(session: AsyncSession, run: Run) -> RecordImport:
    """record_import_rows in this test's session."""
    return partial(record_import_rows, session, run)


type ImportedBank = Callable[..., Awaitable[AccountRecord]]


@pytest.fixture
def imported_bank(run: Run) -> ImportedBank:
    """A manual bank account valued from its transactions (so, as an import's account is),
    optionally a sub-account of `main` and registered under `identifier`."""

    async def _make(
        name: str, *, identifier: str | None = None, main: AccountRecord | None = None
    ) -> AccountRecord:
        account = await run(
            create_account,
            name=name,
            ledger_type="asset",
            subtype="bank",
            balance_mode="transactions",
            parent_account_id=None if main is None else main.id,
        )
        if identifier is not None:
            await run(assign_identifier, account.id, "uk_bank_account", identifier)
        return account

    return _make


type StatementRow = Callable[..., Awaitable[UUID]]


@pytest.fixture
def statement_row(
    session: AsyncSession,
    run: Run,
    gbp: InstrumentRecord,
    ids: SystemIds,  # transfer_contra needs the system accounts
    record_import: RecordImport,
) -> StatementRow:
    """Post a row as an M2 import would, naming nobody (its contra from transfer_contra),
    and record it as a committed import row printing `details`. `identifier_id`: the bank
    account the posted transaction names, which a row naming one does. `key`: the row's
    dedupe key (record_import_rows), its reference; by default one from the transaction.
    `at`: the row's time, which its file gives to `time_unit`; by default `day`'s midnight,
    a date only. `zone`: the time zone its file is read in (its import profile's), which it
    is booked on `at`'s date in; by default the owner's, as posting gives."""

    async def _post(
        account: AccountRecord,
        quantity: str,
        *,
        day: date = DAY,
        details: RowDetails | None = None,
        identifier_id: UUID | None = None,
        bank_transfer: bool = True,
        complete: tuple[date, date] = IMPORTED,
        key: str | None = None,
        at: datetime | None = None,
        time_unit: str = "day",
        zone: str | None = None,
    ) -> UUID:
        occurred_at = datetime.combine(day, time(), UTC) if at is None else at
        contra = await transfer_contra(
            session,
            source=None,
            own_account_id=account.id,
            counterparty_identifier_id=identifier_id,
            other_postings=0,
        )
        amount = Decimal(quantity)
        row = await run(
            post_transaction,
            TransactionInput(
                occurred_at=occurred_at,
                booking_date=None if zone is None else local_date(occurred_at, ZoneInfo(zone)),
                occurred_tz=zone,
                description="Faster payment",
                origin="manual",
                postings=[
                    PostingInput(account.id, gbp.id, amount),
                    PostingInput(contra, gbp.id, -amount),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=identifier_id, bank_transfer=bank_transfer
                ),
            ),
        )
        await record_import(
            account.id,
            [row.id],
            complete,
            keys=None if key is None else [key],
            details=[details or RowDetails()],
            time_unit=time_unit,
        )
        return row.id

    return _post


type DuplicateRow = Callable[..., Awaitable[None]]


@pytest.fixture
def duplicate_row(session: AsyncSession, run: Run) -> DuplicateRow:
    """A later batch's row that duplicates the transaction's committed import row, printing
    `details`: the batch is committed, or rolled back. `key`: the row's own dedupe key, for
    a row matched by its day and amount alone (Decision 16); by default the original's."""

    async def _add(
        transaction_id: UUID,
        details: RowDetails,
        *,
        batch_status: Literal["committed", "rolled_back"] = "committed",
        key: str | None = None,
    ) -> None:
        r = models.ImportRow
        original = (
            await session.execute(
                select(
                    r.id,
                    r.account_id,
                    r.booking_date,
                    r.occurred_at,
                    r.time_unit,
                    r.description,
                    r.quantity,
                    r.instrument_id,
                    r.dedupe_key,
                ).where(r.transaction_id == transaction_id)
            )
        ).one()
        batch_id = await insert_batch(session, run, original.account_id, IMPORTED, batch_status)
        await session.execute(
            insert(r).values(
                batch_id=batch_id,
                account_id=original.account_id,
                position=0,
                kind="statement",
                status="duplicate",
                booking_date=original.booking_date,
                occurred_at=original.occurred_at,
                time_unit=original.time_unit,
                description=original.description,
                quantity=original.quantity,
                instrument_id=original.instrument_id,
                counterparty_sort_code=details.sort_code,
                counterparty_account_number=details.account_number,
                counterparty_account_ending=details.account_ending,
                dedupe_key=original.dedupe_key if key is None else key,
                duplicate_of=original.id,
                raw={},
            )
        )

    return _add
