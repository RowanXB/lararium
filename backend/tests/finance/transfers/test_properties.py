"""Property tests pinning the transfer engine's invariants on generated scenarios
(strategies.py): the owner's accounts, the true movements between and out of them, and the
records that truth produces, arriving in random orders, each arrival followed by heal as
the worker runs it.

- P1 invariant: after every heal, find_transfer_violations and find_unbalanced_transactions
  find nothing.
- P2 idempotence: a heal right after one matches nothing, records no change, emits nothing.
- P3 order independence: without owner actions, the final state is the same for any two
  arrival orders, and the same as posting everything and healing once.
- P4 net worth equals truth: without owner actions, when no transfer needs review and no
  other documented caveat applies (Scenario.p4_caveats, which also names the one design gap
  left to the owner), net worth equals the true net worth on every day, and In transit ends
  at zero.
- P5 decisions survive: with owner actions, after every connector record is upserted again
  (a re-sync) and healed, every rejected record's contra is Unassigned, no never_pair'd
  records are linked, and P1 and P2 still hold.

Each run of a scenario is one transaction on a connection of its own, rolled back at the
end, so examples never see each other. Profiles: "ci" (the default, derandomized, a modest
number of examples) and "deep" (HYPOTHESIS_PROFILE=deep, many more, for local runs)."""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import create_connection
from lararium.db import create_engine
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    post_transaction,
    upsert_connector_transaction,
)
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    candidates,
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    pair,
    reject,
    transfer_contra,
    unpair,
    unreject,
)
from lararium.modules.finance.transfers.queries import OPEN_STATUSES
from lararium.modules.finance.valuation import balances_asof, net_worth_series

from .strategies import (
    FIRST_DAY,
    IDENTIFIERS,
    LAST_DAY,
    MONZO,
    OPENING,
    OPENING_DAY,
    START,
    STRANGERS,
    Account,
    Item,
    Movement,
    Record,
    Scenario,
    Transfer,
    by_day,
    describe,
    local,
    scenarios,
)

settings.register_profile(
    "ci",
    derandomize=True,
    max_examples=15,
    deadline=None,  # every example runs on Postgres
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=int(os.environ.get("HYPOTHESIS_MAX_EXAMPLES", "500")),
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

USER = Actor("user", "test")
WORKER = Actor("system", "transfers")


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class HealOutcome:
    matched: int
    changes: dict[str, Any]
    events: int


@dataclass(frozen=True)
class State:
    """What P3 compares: the final ledger, in the scenario's own names (ids differ from run
    to run)."""

    contras: dict[str, str]  # record key -> the system account its contra is on
    transfers: list[tuple[tuple[str, ...], str, str | None]]
    placeholders: int
    settlements: int
    balances: dict[str, Decimal]  # account name -> balance on the last day


class Ledger:
    """One run of a scenario against the engine, in one session."""

    def __init__(self, session: AsyncSession, scenario: Scenario) -> None:
        self.session = session
        self.scenario = scenario
        self.records = {r.key: r for r in scenario.records}
        self.accounts: dict[str, UUID] = {}
        self.connections: dict[str, UUID] = {}
        self.identifiers: dict[str, UUID] = {}
        self.posted: dict[str, UUID] = {}  # record key -> transaction id
        self.names: dict[UUID, str] = {}  # account id -> name

    async def run(self, fn: Callable[..., Awaitable[Any]], /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(self.session, USER, "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    async def setup(self) -> None:
        self.gbp = (await get_currency(self.session, "GBP")).id
        for key in ("unassigned", "in_transit", "opening_balances"):
            self.names[await self.run(get_or_create_system_account, key)] = key
        self.opening_balances = next(k for k, v in self.names.items() if v == "opening_balances")
        for value in [a.identifier for a in self.scenario.accounts] + list(STRANGERS):
            self.identifiers[value] = await self.run(
                get_or_create_identifier, "uk_bank_account", value
            )
        for account in self.scenario.accounts:
            if account.connected:
                connection = await self.run(
                    create_connection, connector="monzo", display_name=account.name, config={}
                )
                self.connections[account.name] = connection.id
                record = await self.run(
                    create_account,
                    name=account.name,
                    ledger_type="asset",
                    subtype="bank",
                    balance_mode="transactions",
                    source_connection_id=connection.id,
                )
            else:
                record = await self.run(
                    create_account,
                    name=account.name,
                    ledger_type="asset",
                    subtype="bank",
                    balance_mode="hybrid",
                )
            self.accounts[account.name] = record.id
            self.names[record.id] = account.name
        for account in self.scenario.accounts:
            if not account.connected:
                continue
            # A connector registers its own account, and posts its opening balance
            # adjustment against Opening balances, dated before its history.
            await self.run(
                assign_identifier,
                self.accounts[account.name],
                "uk_bank_account",
                account.identifier,
            )
            await self.run(
                upsert_connector_transaction,
                TransactionInput(
                    occurred_at=local(OPENING_DAY),
                    description="Opening balance",
                    origin="connector",
                    connection_id=self.connections[account.name],
                    external_id="opening",
                    postings=[
                        PostingInput(self.accounts[account.name], self.gbp, OPENING),
                        PostingInput(self.opening_balances, self.gbp, -OPENING),
                    ],
                ),
            )

    async def _upsert(self, record: Record) -> None:
        account = record.movement.account
        identifier = None if record.names is None else self.identifiers[record.names]
        contra = await transfer_contra(
            self.session,
            source=("connection", f"{self.connections[account]}:{record.key}"),
            own_account_id=self.accounts[account],
            counterparty_identifier_id=identifier,
            other_postings=0,
        )
        quantity = record.movement.quantity
        posted, _ = await self.run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=record.movement.at,
                description=record.description,
                origin="connector",
                connection_id=self.connections[account],
                external_id=record.key,
                postings=[
                    PostingInput(self.accounts[account], self.gbp, quantity),
                    PostingInput(contra, self.gbp, -quantity),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=identifier, name="Someone", bank_transfer=record.bank_transfer
                ),
            ),
        )
        self.posted[record.key] = posted.id

    async def _import(self, record: Record) -> None:
        account = self.accounts[record.movement.account]
        contra = await transfer_contra(
            self.session,
            source=None,
            own_account_id=account,
            counterparty_identifier_id=None,
            other_postings=0,
        )
        quantity = record.movement.quantity
        posted = await self.run(
            post_transaction,
            TransactionInput(
                occurred_at=record.movement.at,
                description=record.description,
                origin="manual",
                postings=[
                    PostingInput(account, self.gbp, quantity),
                    PostingInput(contra, self.gbp, -quantity),
                ],
                counterparty=CounterpartyFields(bank_transfer=record.bank_transfer),
            ),
        )
        self.posted[record.key] = posted.id

    async def arrive(self, item: Item) -> None:
        what, key = item
        if what == "record":
            record = self.records[key]
            await (self._upsert(record) if record.kind == "connector" else self._import(record))
        elif what == "snapshot":
            account = self.scenario.by_name[key]
            assert account.snapshot_at is not None
            await self.run(
                record_snapshot,
                account_id=self.accounts[key],
                instrument_id=self.gbp,
                quantity=self.scenario.snapshot_quantity(account),
                as_of=account.snapshot_at,
            )
        else:
            account = self.scenario.by_name[key]
            await self.run(
                assign_identifier, self.accounts[key], "uk_bank_account", account.identifier
            )

    async def resync(self) -> None:
        """Every connector record upserted again, as a re-sync would."""
        for record in self.scenario.records:
            if record.kind == "connector":
                await self._upsert(record)

    async def heal(self) -> HealOutcome:
        """heal_transfers in a command of its own, as the worker runs it."""
        captured: list[CommandContext] = []
        async with command_context(self.session, WORKER, "finance.heal_transfers", {}) as ctx:
            captured.append(ctx)
            matched = await heal_transfers(ctx)
        ctx = captured[0]
        return HealOutcome(
            matched,
            dict(ctx._changes),  # pyright: ignore[reportPrivateUsage]
            # The events themselves are expired once committed; how many is what P2 reads.
            len(ctx._events),  # pyright: ignore[reportPrivateUsage]
        )

    async def heal_checked(self, *, idempotent: bool = True) -> None:
        """Heal, then check P1, and P2 unless `idempotent` is False."""
        await self.heal()
        # P1
        assert await find_transfer_violations(self.session) == []
        assert await find_unbalanced_transactions(self.session) == []
        if idempotent:
            # P2
            again = await self.heal()
            assert again == HealOutcome(0, {}, 0), again

    def key_of(self, transaction_id: UUID | None) -> str | None:
        if transaction_id is None:
            return None
        for key, posted in self.posted.items():
            if posted == transaction_id:
                return key
        return f"?{transaction_id}"

    async def contra_of(self, key: str) -> str:
        record = self.records[key]
        contra = await self.session.scalar(
            text(
                "select account_id from finance.postings"
                " where transaction_id = :t and account_id <> :own"
            ),
            {"t": self.posted[key], "own": self.accounts[record.movement.account]},
        )
        return self.names.get(contra, str(contra))

    async def state(self) -> State:
        contras = {key: await self.contra_of(key) for key in sorted(self.posted)}
        transfers = sorted(
            (
                tuple(
                    sorted(
                        k
                        for k in (
                            self.key_of(t.out_transaction_id),
                            self.key_of(t.in_transaction_id),
                        )
                        if k is not None
                    )
                ),
                t.status,
                t.review_reason,
            )
            for t in await list_transfers(self.session)
        )
        rows = await self.session.execute(
            text(
                "select origin, count(*) from finance.transactions"
                " where origin in ('inference', 'rule') and status <> 'void'"
                " group by origin"
            )
        )
        derived = {row[0]: row[1] for row in rows}
        balances = {
            self.names[row.account_id]: row.quantity
            for row in await balances_asof(self.session, LAST_DAY)
            if row.account_id in self.names
        }
        return State(
            contras, transfers, derived.get("inference", 0), derived.get("rule", 0), balances
        )

    async def act(self, kind: str, selector: int, choice: int) -> None:
        """One owner action on the transfers as they stand, picked by `selector` among
        those it applies to (in a stable order), then heal. A pair the engine refuses
        (FinanceValidationError) changes nothing."""
        found = await list_transfers(self.session)
        found.sort(
            key=lambda t: (
                self.key_of(t.out_transaction_id) or "",
                self.key_of(t.in_transaction_id) or "",
            )
        )
        if kind == "reject":
            targets = [t for t in found if t.status != "rejected"]
        elif kind == "unreject":
            targets = [t for t in found if t.status == "rejected"]
        elif kind == "unpair":
            targets = [
                t
                for t in found
                if t.status == "matched" and t.out_transaction_id and t.in_transaction_id
            ]
        else:
            targets = [t for t in found if t.status in OPEN_STATUSES]
        if not targets:
            return
        target = targets[selector % len(targets)]
        note(
            f"owner: {kind} {self.key_of(target.out_transaction_id)}"
            f"/{self.key_of(target.in_transaction_id)}"
        )
        if kind == "reject":
            await self.run(reject, target.id)
        elif kind == "unreject":
            await self.run(unreject, target.id)
        elif kind == "unpair":
            await self.run(unpair, target.id)
        else:
            options = [
                c
                for c in await candidates(self.session, target.id)
                if c.reason in ("exact", "unpaired")
            ]
            if not options:
                return
            chosen = options[choice % len(options)]
            try:
                await self.run(pair, target.id, chosen.transaction_id)
            except FinanceValidationError:
                pass
        await self.heal_checked()

    async def net_worth(self) -> dict[date, Decimal]:
        return {p.day: p.value for p in await net_worth_series(self.session, FIRST_DAY, LAST_DAY)}


@asynccontextmanager
async def isolated(conn: AsyncConnection, scenario: Scenario) -> AsyncIterator[Ledger]:
    """A run of the scenario in a transaction of its own, rolled back afterwards."""
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        ledger = Ledger(session, scenario)
        await ledger.setup()
        yield ledger
    finally:
        await session.close()
        await outer.rollback()


async def _arrive_one_by_one(
    conn: AsyncConnection, scenario: Scenario, order: list[Item], *, every_heal_twice: bool
) -> tuple[State, dict[date, Decimal], bool]:
    """The scenario's items in `order`, each followed by heal, checking P1 after each and P2
    after each (`every_heal_twice`) or the last; returns the final state, the net-worth
    series and whether any transfer needs review."""
    async with isolated(conn, scenario) as ledger:
        for n, item in enumerate(order, start=1):
            await ledger.arrive(item)
            await ledger.heal_checked(idempotent=every_heal_twice or n == len(order))
        state = await ledger.state()
        review = any(t.review_reason for t in await list_transfers(ledger.session))
        return state, await ledger.net_worth(), review


async def _arrive_at_once(conn: AsyncConnection, scenario: Scenario) -> State:
    async with isolated(conn, scenario) as ledger:
        for item in scenario.items():
            await ledger.arrive(item)
        await ledger.heal_checked()
        return await ledger.state()


def _assert_net_worth_is_true(scenario: Scenario, worth: dict[date, Decimal], state: State) -> None:
    """P4: net worth equals the truth on every day, and In transit ends at zero."""
    truth = by_day(scenario)
    wrong = {day: (worth[day], truth[day]) for day in worth if worth[day] != truth[day]}
    assert not wrong, f"net worth (ledger, truth) differs on {wrong}"
    assert state.balances.get("in_transit", Decimal(0)) == 0


@given(scenario=scenarios(), data=st.data())
async def test_matching_is_consistent_order_independent_and_true(
    connection: AsyncConnection, scenario: Scenario, data: st.DataObject
) -> None:
    """P1 to P4."""
    note(describe(scenario))
    event(f"transfers: {min(len(scenario.transfers), 5)}")
    items = scenario.items()
    first = data.draw(st.permutations(items), label="first order")
    second = data.draw(st.permutations(items), label="second order")

    state, worth, review = await _arrive_one_by_one(
        connection, scenario, first, every_heal_twice=True
    )

    # P3
    again, _, _ = await _arrive_one_by_one(connection, scenario, second, every_heal_twice=False)
    assert again == state
    assert await _arrive_at_once(connection, scenario) == state
    # P4
    caveats = scenario.p4_caveats() + (["review"] if review else [])
    event("P4 checked" if not caveats else "P4 skipped: " + caveats[0].split(":")[0])
    if caveats:
        return
    _assert_net_worth_is_true(scenario, worth, state)


@given(scenario=scenarios(owner_actions=True), data=st.data())
async def test_owner_decisions_survive_a_resync(
    connection: AsyncConnection, scenario: Scenario, data: st.DataObject
) -> None:
    """P5 (with P1 and P2 after every heal)."""
    note(describe(scenario))
    event(f"transfers: {min(len(scenario.transfers), 5)}")
    order = data.draw(st.permutations(scenario.items()), label="order")
    async with isolated(connection, scenario) as ledger:
        actions = list(scenario.actions)
        for position in range(len(order) + 1):
            while actions and actions[0][0] == position:
                _, kind, selector, choice = actions.pop(0)
                await ledger.act(kind, selector, choice)
            if position < len(order):
                await ledger.arrive(order[position])
                await ledger.heal_checked()
        await ledger.resync()
        await ledger.heal_checked()

        session = ledger.session
        rows = await session.execute(
            text(
                "select kind, subject_kind, subject_ref, other_kind, other_ref"
                " from finance.transfer_decisions"
            )
        )
        decisions = list(rows)
        event(f"decisions stored: {min(len(decisions), 3)}")

        def resolve(kind: str, ref: str) -> str | None:
            if kind == "connection":
                connection_id, external_id = ref.split(":", 1)
                assert UUID(connection_id) in ledger.connections.values()
                return external_id
            return ledger.key_of(UUID(ref))

        linked = {
            frozenset(
                k
                for k in (ledger.key_of(t.out_transaction_id), ledger.key_of(t.in_transaction_id))
                if k is not None
            )
            for t in await list_transfers(session)
            if t.status != "rejected"
        }
        for d in decisions:
            subject = resolve(d.subject_kind, d.subject_ref)
            if d.kind == "reject" and subject in ledger.posted:
                assert await ledger.contra_of(subject) == "unassigned", subject
            if d.kind == "never_pair":
                other = resolve(d.other_kind, d.other_ref)
                assert frozenset((subject, other)) not in linked, (subject, other)


# Shrunk examples the properties found, kept as plain regression tests.


async def _p4(connection: AsyncConnection, scenario: Scenario) -> None:
    state, worth, review = await _arrive_one_by_one(
        connection, scenario, scenario.items(), every_heal_twice=True
    )
    assert not review
    _assert_net_worth_is_true(scenario, worth, state)


def _scenario(
    *, imported_through: date, snapshot_at: datetime, received_day: date, receipt_day: date
) -> Scenario:
    """Monzo pays Bank 1 50 on Monday 12 October at 00:38, which Bank 1 books on
    received_day; Bank 1 also receives 50 from a friend on receipt_day (a bank transfer
    naming nobody, imported)."""
    sent = Movement(MONZO, Decimal(-50), local(START, time(0, 38)))
    received = Movement("Bank 1", Decimal(50), local(received_day))
    receipt = Movement("Bank 1", Decimal(50), local(receipt_day))
    bank = Account("Bank 1", False, IDENTIFIERS["Bank 1"], snapshot_at, imported_through)
    records = [
        Record("t0-out", "connector", sent, IDENTIFIERS["Bank 1"], True, "Transfer", 0),
        Record("n0", "import", receipt, None, True, "From a friend"),
    ]
    if bank.covers(received.at):
        records.append(Record("t0-in", "import", received, None, True, "Faster payment", 0))
    return Scenario(
        accounts=(Account(MONZO, True, IDENTIFIERS[MONZO]), bank),
        transfers=(Transfer(0, sent, received),),
        noise=(receipt,),
        records=tuple(records),
    )


# Known caveat (design §5.8), accepted until M2 is wired in: when a manual account's own row
# of a transfer is not in the ledger, an unrelated same-amount bank row inside the window is
# taken as it, silently. Wiring M2 closes it with each import batch's date range; these
# tests are strict, so that fix turns them into failures to un-mark.
PENDING_M2_IMPORT_COVERAGE = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="known caveat until M2: an unrelated row stands in for a counterpart not in the ledger",
)


@PENDING_M2_IMPORT_COVERAGE
async def test_unrelated_receipt_stands_in_for_a_counterpart_inside_the_opening_snapshot(
    connection: AsyncConnection,
) -> None:
    """Shrunk from P4: Bank 1 booked the receipt on Sunday, inside its opening snapshot of
    Wednesday 09:00; a friend's 50 on Friday is linked to Monzo's payment instead, so the 50
    sits in In transit from Monday to Thursday."""
    await _p4(
        connection,
        _scenario(
            imported_through=LAST_DAY,
            snapshot_at=local(START + timedelta(days=2), time(9)),
            received_day=START - timedelta(days=1),
            receipt_day=START + timedelta(days=4),
        ),
    )


@PENDING_M2_IMPORT_COVERAGE
async def test_unrelated_receipt_stands_in_for_a_counterpart_after_the_last_import(
    connection: AsyncConnection,
) -> None:
    """The same with Bank 1's own row (a BACS on Saturday) after the last day imported
    (Thursday): the friend's 50 on Wednesday replaces the placeholder, so Bank 1 is
    understated by 50 until the next statement is imported."""
    await _p4(
        connection,
        _scenario(
            imported_through=START + timedelta(days=3),
            snapshot_at=local(START - timedelta(days=5), time(9)),
            received_day=START + timedelta(days=5),
            receipt_day=START + timedelta(days=2),
        ),
    )
