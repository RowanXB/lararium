"""Property tests for the new transfer evidence (transfer evidence design §5.1 to §5.3, §8):
an imported row's account number alone names the registered own account, a sub-account's
bank transfers name its main account, and a sort code alone pairs weakly (pairs.WEAK) but
never opens a transfer of its own.

A scenario (`scenarios`, kept apart from strategies.py) is two or three manual
transactions-mode accounts, each registered under a UK bank account, and a savings
sub-account of the first; one to four transfers between them of 50 or 100 (so they collide),
each evidenced one way (`Evidence`); noise rows of the same amounts naming nobody or a
stranger; and, for any row, a later file's duplicate, with the row's details on the
committed row, on the duplicate or on both. Every account's imports list every scenario day
in full, and every row arrives as an import of its own (statement_row, duplicate_row), each
followed by heal, as an import's commit runs it.

For each scenario and two drawn arrival orders, each run rolled back before the next
(isolated_example), with find_transfer_violations and find_unbalanced_transactions finding
nothing after every heal:
- E1 order independence: the transfers (their accounts, amount, status, review reason and
  the days of their own records) and every record's contra are the same for both orders.
- E2 idempotence: a heal right after one matches nothing (after every heal of the first
  order), and the last changes no transfer and no contra.
- E3 nothing anticipated: after every heal, a placeholder only on an account none of whose
  rows is imported yet (an import lists every scenario day in full), so none at the end.
- E4 weak evidence opens nothing: after every heal, no record whose only evidence is a sort
  code (eligible_records: no holder, a weak_holder) is a transfer's only own record; at the
  end, those records are exactly the rows printing a registered account's sort code alone.
- E5 net worth is true: each account's balance is the sum of its rows, and net worth their
  sum.

Settings and example counts are the "ci" and "deep" profiles' (tests/finance/conftest.py)."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from hypothesis import event, given, note
from hypothesis import strategies as st
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.accounts import AccountRecord
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    TransferRecord,
    list_transfers,
    load_transfer_settings,
)
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import WEAK, pair_relation

from ..importing.staging_helpers import isolated_example
from .conftest import (
    DAY,
    DuplicateRow,
    Heal,
    ImportedBank,
    RowDetails,
    Run,
    StatementRow,
    SystemIds,
    assert_consistent,
    balances_on,
    contra_account,
    net_worth_on,
    placeholders,
    postings_of,
)

REGISTERED = {"A": "101010:10101010", "B": "202020:20202020", "C": "303030:30303030"}
MAIN = "A"
SAVINGS = "S"  # a savings sub-account of MAIN, registered under nothing (it prints nothing)
STRANGER = RowDetails(sort_code="500000", account_number="50000001")  # someone else's
NOTHING = RowDetails()
AMOUNTS = (Decimal(50), Decimal(100))  # tiny on purpose, so transfers collide
START = DAY  # in November: the booking date of a row at 00:00 UTC is its day

type Evidence = Literal[
    "receiver_prints_number",  # the receiving row prints the sender's account number
    "sender_prints_number",  # the sending row prints the receiver's account number
    "main_account",  # one side is the sub-account, and neither row prints anything
    "sender_prints_sort_code",  # the sending row prints the receiver's sort code alone
]


def _evidence(sender: str, receiver: str) -> list[Evidence]:
    """The evidence a transfer between the two can carry: a number or sort code can only
    be a registered account's, and only the sub-account has a main account."""
    found: list[Evidence] = []
    if sender in REGISTERED:
        found.append("receiver_prints_number")
    if receiver in REGISTERED:
        found += ["sender_prints_number", "sender_prints_sort_code"]
    if SAVINGS in (sender, receiver):
        found.append("main_account")
    return found


def _number(account: str) -> RowDetails:
    return RowDetails(account_number=REGISTERED[account].split(":")[1])


def _sort_code(account: str) -> RowDetails:
    return RowDetails(sort_code=REGISTERED[account].split(":")[0])


@dataclass(frozen=True)
class Row:
    """One imported row: a bank transfer, booked on its day, as its committed import row
    and possibly a later file's duplicate of it print it."""

    key: str  # unique in the scenario
    account: str
    quantity: Decimal  # signed
    day: date
    committed: RowDetails  # what its committed import row prints
    duplicate: RowDetails | None  # what a later file's duplicate of it prints; None: none
    weak: bool = False  # its only evidence is the sort code it prints (weak_holder)

    @property
    def identifier(self) -> str | None:
        """The bank account the posted transaction names: the one a full sort code and
        account number on its committed row give, as commit._counterparty reads it."""
        printed = self.committed
        if printed.sort_code is None or printed.account_number is None:
            return None
        return f"{printed.sort_code}:{printed.account_number}"


@dataclass(frozen=True)
class Transfer:
    index: int
    sender: str
    receiver: str
    amount: Decimal
    evidence: Evidence


@dataclass(frozen=True)
class Scenario:
    registered: tuple[str, ...]  # MAIN first
    transfers: tuple[Transfer, ...]
    rows: tuple[Row, ...]

    @property
    def complete(self) -> tuple[date, date]:
        """What every account's imports list in full: from 7 days before the first row to 7
        after the last, so no window (1 day before to 5 after, the default) crosses an
        edge."""
        days = [r.day for r in self.rows]
        return min(days) - timedelta(days=7), max(days) + timedelta(days=7)


def _copies(printed: RowDetails | None) -> st.SearchStrategy[tuple[RowDetails, RowDetails | None]]:
    """What a row's committed import row prints and what a later duplicate of it prints
    (None: no duplicate): the row's details on either of them, or on both."""
    if printed is None:
        return st.sampled_from(((NOTHING, None), (NOTHING, NOTHING)))
    return st.sampled_from(
        ((printed, None), (printed, NOTHING), (NOTHING, printed), (printed, printed))
    )


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    registered = tuple(REGISTERED)[: draw(st.integers(2, 3))]
    pairs = [(x, y) for x in registered for y in registered if x != y]
    # A savings account moves money only to and from its main account (design §1).
    pairs += [(MAIN, SAVINGS), (SAVINGS, MAIN)]
    transfers: list[Transfer] = []
    rows: list[Row] = []
    for index in range(draw(st.integers(1, 4))):
        sender, receiver = draw(st.sampled_from(pairs))
        evidence = draw(st.sampled_from(_evidence(sender, receiver)))
        amount = draw(st.sampled_from(AMOUNTS))
        sent = START + timedelta(days=draw(st.integers(0, 3)))
        # Inside the sending row's window, mostly the same day.
        received = sent + timedelta(days=draw(st.sampled_from((0, 0, 1, 2, -1))))
        # What the sending and the receiving row print.
        out: RowDetails | None = None
        in_: RowDetails | None = None
        if evidence == "sender_prints_number":
            out = _number(receiver)
        elif evidence == "sender_prints_sort_code":
            out = _sort_code(receiver)
        elif evidence == "receiver_prints_number":
            in_ = _number(sender)
        weak = evidence == "sender_prints_sort_code"
        transfers.append(Transfer(index, sender, receiver, amount, evidence))
        rows.append(Row(f"t{index}-out", sender, -amount, sent, *draw(_copies(out)), weak=weak))
        rows.append(Row(f"t{index}-in", receiver, amount, received, *draw(_copies(in_))))
    for index in range(draw(st.integers(0, 3))):
        account = draw(st.sampled_from(registered))
        quantity = draw(st.sampled_from(AMOUNTS)) * draw(st.sampled_from((-1, 1)))
        day = START + timedelta(days=draw(st.integers(0, 5)))
        printed = STRANGER if draw(st.booleans()) else None
        rows.append(Row(f"n{index}", account, quantity, day, *draw(_copies(printed))))
    return Scenario(registered, tuple(transfers), tuple(rows))


type Item = tuple[Literal["row", "duplicate"], str]


@st.composite
def arrivals(draw: st.DrawFn, scenario: Scenario) -> list[Item]:
    """An order the rows arrive in, each in an import of its own: a duplicate after the
    row it duplicates."""
    keys = draw(st.permutations([r.key for r in scenario.rows]))
    order: list[Item] = [("row", key) for key in keys]
    for row in scenario.rows:
        if row.duplicate is not None:
            after = order.index(("row", row.key)) + 1
            order.insert(draw(st.integers(after, len(order))), ("duplicate", row.key))
    return order


def _printed(details: RowDetails) -> str:
    parts = [
        f"{name} {value}"
        for name, value in (("sort code", details.sort_code), ("number", details.account_number))
        if value is not None
    ]
    return ", ".join(parts) or "nothing"


def describe(scenario: Scenario) -> str:
    """The scenario, readable, for a failing example's report."""
    lines = [
        f"registered: {', '.join(scenario.registered)}; {SAVINGS} is {MAIN}'s sub-account;"
        f" imports list {scenario.complete[0]} to {scenario.complete[1]} in full"
    ]
    for t in scenario.transfers:
        lines.append(f"transfer {t.index}: {t.sender} -> {t.receiver} {t.amount}, {t.evidence}")
    for r in scenario.rows:
        duplicate = "" if r.duplicate is None else f", its duplicate {_printed(r.duplicate)}"
        lines.append(
            f"row {r.key}: {r.account} {r.quantity} on {r.day:%a %d %b},"
            f" prints {_printed(r.committed)}{duplicate}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class Harness:
    """The conftest's fixtures a run of a scenario uses."""

    session: AsyncSession
    run: Run
    ids: SystemIds
    imported_bank: ImportedBank
    statement_row: StatementRow
    duplicate_row: DuplicateRow
    heal: Heal


@dataclass(frozen=True)
class Outcome:
    """What E1 compares, in the scenario's names (ids differ from run to run)."""

    transfers: tuple[str, ...]  # sorted
    contras: dict[str, str]  # row key -> the system account its contra is on


async def _weak_only(session: AsyncSession) -> set[UUID]:
    """The own records whose only evidence is a sort code: they name no other own account
    (eligible_records' holder), and a sort code alone names one (their weak_holder)."""
    e = eligible_records(await system_account_ids(session))
    found = await session.scalars(
        select(e.c.transaction_id).where(
            or_(e.c.holder.is_(None), e.c.holder == e.c.account_id), e.c.weak_holder.is_not(None)
        )
    )
    return set(found)


async def _check_heal(session: AsyncSession, imported: set[UUID]) -> None:
    """What holds after every heal, whatever has arrived: M4's invariants, E3 (a placeholder
    only on an account with no imports yet) and E4 (no record whose only evidence is a sort
    code is a transfer's only own record)."""
    await assert_consistent(session)
    for placeholder in await placeholders(session):
        on = {account for account, _ in await postings_of(session, placeholder.id)}
        assert not on & imported, f"{placeholder.description} beside an imported row"
    weak = await _weak_only(session)
    for t in await list_transfers(session):
        own = [x for x in (t.out_transaction_id, t.in_transaction_id) if x is not None]
        assert not (len(own) == 1 and own[0] in weak), f"a sort code alone opened {t}"


async def _run(
    h: Harness, scenario: Scenario, order: list[Item], *, every_heal_twice: bool
) -> tuple[Outcome, set[str]]:
    """One run of the scenario, its rows arriving in `order`, each followed by heal (and, if
    `every_heal_twice`, another that must match nothing): checks what holds after every
    heal (_check_heal), then E2 to E5 at the end; returns what E1 compares and what the run
    exercised (for event())."""
    session = h.session
    accounts: dict[str, AccountRecord] = {}
    for name in scenario.registered:
        accounts[name] = await h.imported_bank(name, identifier=REGISTERED[name])
    accounts[SAVINGS] = await h.imported_bank(SAVINGS, main=accounts[MAIN])
    rows = {r.key: r for r in scenario.rows}
    posted: dict[str, UUID] = {}  # row key -> transaction id
    imported: set[UUID] = set()  # the accounts with an import (each lists the scenario's days)
    for what, key in order:
        row = rows[key]
        if what == "row":
            identifier_id = None
            if row.identifier is not None:
                identifier_id = await h.run(
                    get_or_create_identifier, "uk_bank_account", row.identifier
                )
            posted[key] = await h.statement_row(
                accounts[row.account],
                str(row.quantity),
                day=row.day,
                details=row.committed,
                identifier_id=identifier_id,
                complete=scenario.complete,
            )
            imported.add(accounts[row.account].id)
        else:
            assert row.duplicate is not None
            await h.duplicate_row(posted[key], row.duplicate)
        await h.heal()
        await _check_heal(session, imported)
        if every_heal_twice:
            assert await h.heal() == 0  # E2

    async def state() -> tuple[list[TransferRecord], dict[str, UUID]]:
        transfers = sorted(await list_transfers(session), key=lambda t: t.id)
        contras = {
            key: await contra_account(session, transaction_id, accounts[rows[key].account].id)
            for key, transaction_id in posted.items()
        }
        return transfers, contras

    # E2
    before = await state()
    assert await h.heal() == 0
    assert await state() == before
    transfers, contras = before
    # E3
    assert await placeholders(session) == []
    # E4, its property checked after the last heal: the records it is about are the rows
    # printing a registered account's sort code alone.
    weak = await _weak_only(session)
    assert weak == {posted[r.key] for r in scenario.rows if r.weak}
    # E5
    day = scenario.complete[1]
    balances = await balances_on(session, day)
    for name, account in accounts.items():
        truth = sum((r.quantity for r in scenario.rows if r.account == name), Decimal(0))
        assert balances.get(account.id, Decimal(0)) == truth, name
    assert await net_worth_on(session, day) == sum((r.quantity for r in scenario.rows), Decimal(0))

    names = {a.id: name for name, a in accounts.items()}
    names |= {h.ids.unassigned: "Unassigned", h.ids.in_transit: "In transit"}
    day_of = {transaction_id: rows[key].day for key, transaction_id in posted.items()}

    def leg(transaction_id: UUID | None) -> str:
        return "-" if transaction_id is None else f"{day_of[transaction_id]:%a %d}"

    outcome = Outcome(
        tuple(
            sorted(
                f"{names[t.from_account_id]} -> {names[t.to_account_id]} {t.amount} {t.status}"
                f" ({t.review_reason or 'no review'}), out {leg(t.out_transaction_id)},"
                f" in {leg(t.in_transaction_id)}"
                for t in transfers
            )
        ),
        {key: names.get(contra, str(contra)) for key, contra in contras.items()},
    )
    linked = {
        x for t in transfers for x in (t.out_transaction_id, t.in_transaction_id) if x is not None
    }
    exercised = {f"status {t.status}" for t in transfers}
    exercised |= {"review" for t in transfers if t.review_reason}
    relation = await pair_relation(
        session, await system_account_ids(session), await load_transfer_settings(session)
    )
    if any(
        t.out_transaction_id is not None
        and t.in_transaction_id is not None
        and relation.tiers.get(t.out_transaction_id, {}).get(t.in_transaction_id) == WEAK
        for t in transfers
    ):
        exercised.add("a sort code alone paired two rows (pairs.WEAK)")
    if weak - linked:
        exercised.add("a row printing a sort code alone stayed unpaired")
    return outcome, exercised


@given(scenario=scenarios(), data=st.data())
async def test_new_evidence_is_order_independent_idempotent_and_never_anticipates_an_imported_row(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    duplicate_row: DuplicateRow,
    heal: Heal,
    scenario: Scenario,
    data: st.DataObject,
) -> None:
    """E1 to E5."""
    note(describe(scenario))
    h = Harness(session, run, ids, imported_bank, statement_row, duplicate_row, heal)
    first = data.draw(arrivals(scenario), label="first order")
    second = data.draw(arrivals(scenario), label="second order")
    async with isolated_example(session):
        outcome, exercised = await _run(h, scenario, first, every_heal_twice=True)
    async with isolated_example(session):
        again, _ = await _run(h, scenario, second, every_heal_twice=False)
    # E1
    assert again == outcome

    event(f"transfers: {len(scenario.transfers)}")
    for evidence in sorted({t.evidence for t in scenario.transfers}):
        event(f"evidence: {evidence}")
    if any(r.committed == NOTHING and r.duplicate not in (None, NOTHING) for r in scenario.rows):
        event("details printed on a duplicate only")
    for label in sorted(exercised):
        event(label)
