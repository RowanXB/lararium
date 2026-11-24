"""Property tests for cross-currency transfers (eToro design §5.12) on generated scenarios
(strategies.cross_currency_scenarios): eToro Money's GBP and eToro Trading's USD legs of
moves between them, mutually identified and within the near window, with or without the
statement's conversion fee; and, with owner actions, payouts from Trading to a UK bank that
only the owner can pair. Each leg arrives as a connector record (transfer_contra, then
upsert_connector_transaction, its leg fee in transfer_leg_fees), each arrival followed by
heal as the worker runs it.

- X1 invariant: after every heal, find_transfer_violations and find_unbalanced_transactions
  find nothing, and the live settlements are exactly the matched transfers' (so an unpair or
  a dissolved match leaves none behind).
- X2 idempotence: a heal right after one matches nothing, records nothing, emits nothing.
- X3 order independence: without owner actions, the final state (each record's contra, the
  transfers, their settlements' postings, every balance per currency) is the same for any
  two arrival orders and for posting everything then healing once; every move is matched,
  and In transit nets to zero in each currency.
- X4 regeneration: with the transfers, derived transactions and connector records deleted
  (the owner's decisions kept), posting the records again and healing gives the same state.
- X5 owner decisions: pair and unpair at drawn points of the arrival order (each applied
  when both records are in, else dropped, as the owner can't act on a record not yet
  there). The end state, decisions included, is the one the final decision on each
  transfer gives alone, applied once everything has arrived; an unpair leaves no settlement.

Each run is a transaction on a connection of its own, rolled back at the end. Profiles are
tests/finance/conftest.py's."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from hypothesis import event, given, note, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import create_connection
from lararium.db import create_engine
from lararium.modules.finance.accounts import create_account
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.identifiers import assign_identifier
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    heal_transfers,
    list_transfers,
    pair_records,
    transfer_contra,
    unpair,
)

from .strategies import (
    BANK,
    CROSS_START,
    MONEY,
    TRADING,
    CrossScenario,
    Leg,
    cross_currency_scenarios,
    describe_cross,
)

USER = Actor("user", "test")
WORKER = Actor("system", "transfers")
IDENTIFIERS = {MONEY: "1234567", TRADING: "7654321"}  # etoro_account values
BANK_IDENTIFIER = "112233:44556677"
CURRENCY = {MONEY: "GBP", TRADING: "USD", BANK: "GBP"}
OPENING_AT = CROSS_START - timedelta(days=10)
LAST_DAY = (CROSS_START + timedelta(days=5)).date()


@pytest.fixture
async def connection(backend_url: str, migrated: None) -> AsyncIterator[AsyncConnection]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


def _openings() -> list[Leg]:
    """A deposit naming nobody on each account, well before the scenario: each history
    starts there."""
    return [
        Leg(f"opening:{name}", name, CURRENCY[name], Decimal(5000), OPENING_AT, None)
        for name in (MONEY, TRADING, BANK)
    ]


@dataclass(frozen=True)
class HealOutcome:
    matched: int
    changes: dict[str, Any]
    events: int


class Books:
    """One run of a cross-currency scenario against the engine, in one session."""

    def __init__(self, session: AsyncSession, scenario: CrossScenario) -> None:
        self.session = session
        self.scenario = scenario
        self.legs = {leg.key: leg for leg in (*_openings(), *scenario.legs)}
        self.accounts: dict[str, UUID] = {}
        self.connections: dict[str, UUID] = {}
        self.identifiers: dict[str, UUID] = {}
        self.currencies: dict[str, UUID] = {}
        self.names: dict[UUID, str] = {}
        self.posted: dict[str, UUID] = {}

    async def run(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(self.session, USER, "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    async def setup(self) -> None:
        for code in ("GBP", "USD"):
            currency = await get_currency(self.session, code)
            self.currencies[code] = currency.id
            self.names[currency.id] = code
        for key in ("unassigned", "in_transit", "opening_balances"):
            self.names[await self.run(get_or_create_system_account, key)] = key
        for name in (MONEY, TRADING, BANK):
            connection = await self.run(
                create_connection, connector="etoro", display_name=name, config={}
            )
            account = await self.run(
                create_account,
                name=name,
                ledger_type="asset",
                subtype="bank" if name != TRADING else "broker",
                balance_mode="transactions",
                source_connection_id=connection.id,
            )
            self.connections[name] = connection.id
            self.accounts[name] = account.id
            self.names[account.id] = name
        for name, value in IDENTIFIERS.items():
            identifier = await self.run(
                assign_identifier, self.accounts[name], "etoro_account", value
            )
            self.identifiers[name] = identifier.id
        await self.run(assign_identifier, self.accounts[BANK], "uk_bank_account", BANK_IDENTIFIER)

    async def arrive(self, key: str) -> None:
        """The leg as its connector posts it: its contra from transfer_contra, its leg fee."""
        leg = self.legs[key]
        identifier = None if leg.names is None else self.identifiers[leg.names]
        connection = self.connections[leg.account]
        contra = await transfer_contra(
            self.session,
            source=("connection", f"{connection}:{leg.key}"),
            own_account_id=self.accounts[leg.account],
            counterparty_identifier_id=identifier,
            other_postings=0,
        )
        currency = self.currencies[leg.currency]
        posted, _ = await self.run(
            upsert_connector_transaction,
            TransactionInput(
                occurred_at=leg.at,
                description="Transfer",
                origin="connector",
                connection_id=connection,
                external_id=leg.key,
                postings=[
                    PostingInput(self.accounts[leg.account], currency, leg.quantity),
                    PostingInput(contra, currency, -leg.quantity),
                ],
                counterparty=CounterpartyFields(
                    identifier_id=identifier, bank_transfer=leg.bank_transfer
                ),
            ),
        )
        await self.session.execute(
            text("delete from finance.transfer_leg_fees where transaction_id = :t"),
            {"t": posted.id},
        )
        if leg.fee is not None:
            await self.session.execute(
                text(
                    "insert into finance.transfer_leg_fees (transaction_id, instrument_id, amount)"
                    " values (:t, :i, :a)"
                ),
                {"t": posted.id, "i": self.currencies["USD"], "a": leg.fee},
            )
        self.posted[key] = posted.id

    def items(self) -> list[str]:
        return list(self.legs)

    async def heal(self) -> HealOutcome:
        captured: list[CommandContext] = []
        async with command_context(self.session, WORKER, "finance.heal_transfers", {}) as ctx:
            captured.append(ctx)
            matched = await heal_transfers(ctx)
        ctx = captured[0]
        return HealOutcome(
            matched,
            dict(ctx._changes),  # pyright: ignore[reportPrivateUsage]
            len(ctx._events),  # pyright: ignore[reportPrivateUsage]
        )

    async def heal_checked(self, *, idempotent: bool = True) -> None:
        await self.heal()
        await self.check()
        if idempotent:
            again = await self.heal()
            assert again == HealOutcome(0, {}, 0), again  # X2

    async def check(self) -> None:
        """X1."""
        assert await find_transfer_violations(self.session) == []
        assert await find_unbalanced_transactions(self.session) == []
        live = set(
            await self.session.scalars(
                text(
                    "select id from finance.transactions where origin = 'rule' and status <> 'void'"
                )
            )
        )
        settled = {
            t.settlement_transaction_id
            for t in await list_transfers(self.session)
            if t.status == "matched" and t.settlement_transaction_id is not None
        }
        assert live == settled, (live, settled)

    def key_of(self, transaction_id: UUID | None) -> str | None:
        if transaction_id is None:
            return None
        return next(
            (k for k, v in self.posted.items() if v == transaction_id), f"?{transaction_id}"
        )

    async def transfer_of(self, index: int) -> Any | None:
        """The transfer linking the scenario's transfer's two records, if any."""
        t = self.scenario.transfers[index]
        ids = {self.posted.get(t.sent), self.posted.get(t.received)}
        for transfer in await list_transfers(self.session):
            if {transfer.out_transaction_id, transfer.in_transaction_id} == ids:
                return transfer
        return None

    async def state(self) -> dict[str, Any]:
        """The final ledger in the scenario's names."""
        contras: dict[str, str] = {}
        for key, transaction_id in sorted(self.posted.items()):
            contra = await self.session.scalar(
                text(
                    "select account_id from finance.postings"
                    " where transaction_id = :t and account_id <> :own"
                ),
                {"t": transaction_id, "own": self.accounts[self.legs[key].account]},
            )
            contras[key] = self.names.get(contra, str(contra))
        transfers = []
        for t in await list_transfers(self.session):
            settlement: list[tuple[Any, ...]] = []
            if t.settlement_transaction_id is not None:
                rows = await self.session.execute(
                    text(
                        "select account_id, instrument_id, quantity, price_per_unit,"
                        " price_instrument_id, is_fee from finance.postings"
                        " where transaction_id = :t order by position"
                    ),
                    {"t": t.settlement_transaction_id},
                )
                settlement = [
                    tuple(self.names.get(v, v) if isinstance(v, UUID) else v for v in row)
                    for row in rows
                ]
            transfers.append(
                (
                    self.key_of(t.out_transaction_id),
                    self.key_of(t.in_transaction_id),
                    t.status,
                    t.review_reason,
                    self.names.get(t.instrument_id),
                    t.amount,
                    tuple(settlement),
                )
            )
        rows = await self.session.execute(
            text(
                "select account_id, instrument_id, quantity from finance.v1_balances_asof(:d)"
                " where quantity <> 0"
            ),
            {"d": LAST_DAY},
        )
        balances = {
            (self.names[r.account_id], self.names[r.instrument_id]): r.quantity
            for r in rows
            if r.account_id in self.names
        }
        return {
            "contras": contras,
            "transfers": sorted(transfers, key=repr),
            "balances": balances,
            "decisions": await self.decisions(),
        }

    async def decisions(self) -> list[tuple[str, str, str]]:
        """The owner's decisions, by the records' keys."""
        rows = await self.session.execute(
            text("select kind, subject_ref, other_ref from finance.transfer_decisions")
        )
        return sorted(
            (r.kind, r.subject_ref.split(":", 1)[1], r.other_ref.split(":", 1)[1]) for r in rows
        )

    async def in_transit(self) -> dict[str, Decimal]:
        state = await self.state()
        return {c: q for (a, c), q in state["balances"].items() if a == "in_transit"}

    async def act(self, kind: str, index: int) -> bool:
        """The owner's pair or unpair of the scenario's transfer, when it can be made: both
        records in, and the pair apart (pair) or matched (unpair). Whether it was made."""
        t = self.scenario.transfers[index]
        if t.sent not in self.posted or t.received not in self.posted:
            return False
        found = await self.transfer_of(index)
        matched = found is not None and found.status == "matched"
        if kind == "unpair" and matched:
            settlement = found.settlement_transaction_id
            await self.run(unpair, found.id)
            # An unpair leaves no settlement.
            left = await self.session.scalar(
                text(
                    "select count(*) from finance.transactions where id = :t and status <> 'void'"
                ),
                {"t": settlement},
            )
            assert left == 0
        elif kind == "pair" and not matched:
            try:
                await self.run(pair_records, self.posted[t.sent], self.posted[t.received])
            except FinanceValidationError as refused:
                raise AssertionError(f"the owner's pair was refused: {refused}") from None
        else:
            return False
        note(f"owner: {kind} transfer {index}")
        await self.heal_checked()
        return True

    async def regenerate(self) -> None:
        """X4: everything derived deleted, the owner's decisions kept, the records posted
        again in the canonical order, then one heal."""
        await self.session.execute(text("delete from finance.transfers"))
        await self.session.execute(text("delete from finance.transfer_leg_fees"))
        await self.session.execute(
            text("delete from finance.transactions where origin in ('rule', 'inference')")
        )
        await self.session.execute(
            text("delete from finance.transactions where connection_id = any(:c)"),
            {"c": list(self.connections.values())},
        )
        self.posted.clear()
        for key in self.items():
            await self.arrive(key)
        await self.heal_checked()


@asynccontextmanager
async def isolated(conn: AsyncConnection, scenario: CrossScenario) -> AsyncIterator[Books]:
    outer = await conn.begin()
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        books = Books(session, scenario)
        await books.setup()
        yield books
    finally:
        await session.close()
        await outer.rollback()


async def _one_by_one(
    conn: AsyncConnection, scenario: CrossScenario, order: list[str], *, every_heal_twice: bool
) -> tuple[dict[str, Any], dict[str, Decimal]]:
    async with isolated(conn, scenario) as books:
        for n, key in enumerate(order, start=1):
            await books.arrive(key)
            await books.heal_checked(idempotent=every_heal_twice or n == len(order))
        return await books.state(), await books.in_transit()


# Each example replays its scenario four times on Postgres: half the profile's examples (a
# third for X5, which acts on fewer transfers) keeps CI's time near the other properties'.
@settings(max_examples=max(1, settings().max_examples // 2))
@given(scenario=cross_currency_scenarios(), data=st.data())
async def test_cross_currency_pairs_are_order_independent_and_net_to_zero(
    connection: AsyncConnection, scenario: CrossScenario, data: st.DataObject
) -> None:
    """X1 to X4."""
    note(describe_cross(scenario))
    event(f"moves: {len(scenario.transfers)}")
    event(f"with a fee: {any(leg.fee is not None for leg in scenario.legs)}")
    async with isolated(connection, scenario) as books:
        items = books.items()
    first = data.draw(st.permutations(items), label="first order")
    second = data.draw(st.permutations(items), label="second order")

    state, in_transit = await _one_by_one(connection, scenario, first, every_heal_twice=True)
    # Every move is matched, with its conversion, and In transit nets to zero per currency.
    matched = [t for t in state["transfers"] if t[2] == "matched"]
    assert len(matched) == len(scenario.transfers), state["transfers"]
    assert all(t[6] for t in matched)  # each has its conversion's postings
    assert in_transit == {}
    # X3
    again, _ = await _one_by_one(connection, scenario, second, every_heal_twice=False)
    assert again == state
    async with isolated(connection, scenario) as books:
        for key in books.items():
            await books.arrive(key)
        await books.heal_checked()
        assert await books.state() == state
        # X4
        await books.regenerate()
        assert await books.state() == state


def _final_actions(
    effective: list[tuple[str, int]], scenario: CrossScenario
) -> list[tuple[str, int]]:
    """The shortest sequence of owner actions, applied once everything has arrived, that ends
    each transfer on its last effective decision: a move the engine pairs needs an unpair
    before a pair; a payout it can't pair needs a pair before an unpair."""
    last: dict[int, str] = {}
    for kind, index in effective:
        last[index] = kind
    found: list[tuple[str, int]] = []
    for index, kind in sorted(last.items()):
        auto = scenario.transfers[index].kind == "move"
        if kind == "pair":
            found += [("unpair", index), ("pair", index)] if auto else [("pair", index)]
        else:
            found += [("unpair", index)] if auto else [("pair", index), ("unpair", index)]
    return found


@settings(max_examples=max(1, settings().max_examples // 3))
@given(scenario=cross_currency_scenarios(owner_actions=True), data=st.data())
async def test_owner_pairs_and_unpairs_depend_only_on_the_final_decisions(
    connection: AsyncConnection, scenario: CrossScenario, data: st.DataObject
) -> None:
    """X5 (with X1 and X2 after every heal), and X4 with the decisions."""
    note(describe_cross(scenario))
    async with isolated(connection, scenario) as books:
        items = books.items()
    order = data.draw(st.permutations(items), label="order")
    effective: list[tuple[str, int]] = []
    async with isolated(connection, scenario) as books:
        actions = list(scenario.actions)
        for position in range(len(order) + 1):
            while actions and actions[0][0] == position:
                _, kind, index = actions.pop(0)
                if await books.act(kind, index):
                    effective.append((kind, index))
            if position < len(order):
                await books.arrive(order[position])
                await books.heal_checked()
        state = await books.state()
        await books.regenerate()
        assert await books.state() == state
    event(f"owner actions made: {min(len(effective), 3)}")
    event(f"payouts: {sum(t.kind == 'payout' for t in scenario.transfers)}")

    final = _final_actions(effective, scenario)
    note(f"final decisions alone: {final}")
    async with isolated(connection, scenario) as books:
        for key in books.items():
            await books.arrive(key)
        await books.heal_checked()
        for kind, index in final:
            assert await books.act(kind, index), (kind, index)
        assert await books.state() == state
