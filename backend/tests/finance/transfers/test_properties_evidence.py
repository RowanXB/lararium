"""Property tests for the new transfer evidence (transfer evidence design §5.1 to §5.3, §8):
an imported row's account number alone names the registered own account, a sub-account's
bank transfers name its main account, and a sort code alone pairs weakly (pairs.WEAK) but
never opens a transfer of its own.

A scenario (`scenarios`, kept apart from strategies.py):
- Accounts: two or three manual transactions-mode accounts, each registered under a UK bank
  account; a savings sub-account of the first; and, in some, a Monzo-like account with a
  connection of its own, registered under its full identifier, whose history starts well
  before the scenario.
- Transfers: one to four between them of 50 or 100 (so they collide), each evidenced one way
  (`Evidence`). A manual row naming the connected account prints only its account number,
  or as the sender only its sort code (as Revolut and BOC rows print Monzo's); the
  connected account's record names the manual account in full, names nobody, or never
  arrives (`Synced`).
- Noise of the same amounts: on the registered accounts, rows naming nobody or a stranger;
  on the sub-account, rows naming nobody (so its main account) or printing part of a bank
  account only: a stranger's sort code or account number, or a registered account's sort
  code.
- Duplicates: for any imported row, a later file's duplicate, with the row's details on the
  committed row, on the duplicate or on both.

A run (`Example`): the rows arrive in a drawn order, each an import of its own (every
account's imports list every scenario day in full) or a sync of the connected account's
record, each followed by heal, as an import's commit or a sync runs it. Then the owner acts
once (`Scenario.action`), on a transfer picked by its rows' keys: rejects it, or unpairs a
matched one, one a sort code alone made too. A later file then duplicates one of the rows (the
acted-on transfer's, when it has one), and the connected account's records are synced again.
Rows' import references are their keys, so a run's twins pair alike (pairs._twins).

For each scenario and two drawn arrival orders, each run rolled back before the next
(isolated_example), with find_transfer_violations and find_unbalanced_transactions finding
nothing after every heal:
- E1 order independence: after the arrivals, and again at the end, the transfers (their
  accounts, amount, status, review reason and their own records) and every record's contra
  are the same for both orders.
- E2 idempotence: a heal right after one matches nothing (after every heal of the first
  order); the last heal of the arrivals and the re-sync change no transfer and no contra.
- E3 nothing anticipated: after every heal, a placeholder only on an account none of whose
  rows is imported yet, so none after the arrivals, nor at the end.
- E4 weak evidence opens nothing: after every heal, no record whose only evidence is a sort
  code (eligible_records: no holder, a weak_holder) is a transfer's only own record; after
  the arrivals and at the end, those records are exactly the rows printing a registered
  account's sort code alone and no account number, the late duplicate's print included.
- E5 net worth is true: each account's balance is the sum of its rows. After the arrivals,
  net worth is the sum of every true movement, the connected account's records never
  synced included where a row naming it by account number waits for them (In transit holds
  them; a sort code alone never waits, design §5.3), unless a transfer of the connected
  account needs review (In transit then also holds what waits for the owner, M4's P4
  caveat).
- E6 a record still to sync is awaited: a row naming the connected account by account
  number, whose record there never arrives and which has no other candidate there, waits
  for it: its own transfer is awaiting, with no review.
- E7 decisions survive a later file and a re-sync: a rejected transfer stays rejected, its
  records on Unassigned, and unpaired records are never linked again.

Accounts funded by bank transfer (account type catalog design §8.2; `funding_scenarios`):
records naming a bank account, registered to an account or to none (`payee_scenarios`),
beside other sides naming nobody (some printing the connected account's sort code alone) and
noise, some rows not marked bank transfers, run once as it is and once with a drawn set of
accounts given a type funded by bank transfer:
- E10 funding changes only the pairs of unmarked records naming nobody on those accounts:
  every other same-currency edge of the pair relation (card and cross-currency edges are
  outside it), with its tier, is the same in both runs, and each one funding adds is
  ONE_SIDED, with a record naming the deposit's account.

Settings and example counts are the "ci" and "deep" profiles' (tests/finance/conftest.py)."""

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import pytest
from hypothesis import event, given, note
from hypothesis import strategies as st
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.modules.finance.account_types import keys_funded_by
from lararium.modules.finance.accounts import AccountRecord, set_type_key
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import (
    TransferRecord,
    TransferSettings,
    list_transfers,
    load_transfer_settings,
    reject,
    unpair,
)
from lararium.modules.finance.transfers.contra import eligible_records
from lararium.modules.finance.transfers.pairs import ONE_SIDED, WEAK, pair_relation

from ..importing.staging_helpers import isolated_example
from .conftest import (
    DAY,
    Connected,
    ConnectorTx,
    DuplicateRow,
    Heal,
    ImportedBank,
    MakeConnected,
    RowDetails,
    Run,
    StatementRow,
    SystemIds,
    assert_consistent,
    balances_on,
    contra_account,
    history,
    net_worth_on,
    placeholders,
    postings_of,
)

REGISTERED = {"A": "101010:10101010", "B": "202020:20202020", "C": "303030:30303030"}
MAIN = "A"
SAVINGS = "S"  # a savings sub-account of MAIN, registered under nothing (it prints nothing)
CONNECTED = "M"  # a Monzo-like account with a connection of its own
CONNECTED_ID = "606060:60606060"  # registered to it, as its connector registers it
HISTORY = Decimal(-5)  # the card payment its history starts with (conftest.history)
CARD = Decimal(-5)  # a card payment: no bank transfer, so it never pairs
STRANGER = RowDetails(sort_code="500000", account_number="50000001")  # someone else's
# Part of someone else's bank account: it names no own account.
STRANGER_SORT_CODE = RowDetails(sort_code="500000")
STRANGER_NUMBER = RowDetails(account_number="50000001")
NOTHING = RowDetails()
AMOUNTS = (Decimal(50), Decimal(100))  # tiny on purpose, so transfers collide
START = DAY  # in November: the booking date of a row at 00:00 UTC is its day

type Evidence = Literal[
    "receiver_prints_number",  # the receiving row prints the sender's account number
    "sender_prints_number",  # the sending row prints the receiver's account number
    "main_account",  # one side is the sub-account, and neither row prints anything
    "sender_prints_sort_code",  # the sending row prints the receiver's sort code alone
]
# The connected account's record of a transfer: it names the manual account in full, names
# nobody, or never arrives.
type Synced = Literal["names", "nobody", "missing"]
type Action = Literal["reject", "unpair"]
# What the late duplicate prints: nothing, what its row prints, or the account number of
# the row's other side (a stranger's, for noise).
type LatePrint = Literal["nothing", "same", "number"]


def _identifier(account: str) -> str:
    return CONNECTED_ID if account == CONNECTED else REGISTERED[account]


def _numbered(account: str) -> bool:
    """Whether a row can print the account's number or sort code: it is registered."""
    return account in REGISTERED or account == CONNECTED


def _evidence(sender: str, receiver: str) -> list[Evidence]:
    """The evidence a transfer between the two can carry: a number or sort code can only be
    a registered account's and printed by an import row (a connected account's record names
    a bank account in full, or nobody), and only the sub-account has a main account."""
    found: list[Evidence] = []
    if _numbered(sender) and receiver != CONNECTED:
        found.append("receiver_prints_number")
    if _numbered(receiver) and sender != CONNECTED:
        found += ["sender_prints_number", "sender_prints_sort_code"]
    if SAVINGS in (sender, receiver):
        found.append("main_account")
    return found


def _number(account: str) -> RowDetails:
    return RowDetails(account_number=_identifier(account).split(":")[1])


def _sort_code(account: str) -> RowDetails:
    return RowDetails(sort_code=_identifier(account).split(":")[0])


@dataclass(frozen=True)
class Row:
    """One row: an imported one, as its committed import row and possibly a later file's
    duplicate of it print it; or the connected account's record, which prints nothing but
    may name a bank account in full (`names`, the own account it is registered to)."""

    key: str  # unique in the scenario: the row's import reference, or its external id
    account: str
    quantity: Decimal  # signed
    day: date
    committed: RowDetails = NOTHING  # what its committed import row prints
    duplicate: RowDetails | None = None  # what a later file's duplicate of it prints
    names: str | None = None  # the connected account's record: whose account it names
    transfer: int | None = None  # the transfer it is a side of; None for noise
    other: str | None = None  # that transfer's other side
    bank_transfer: bool = True

    @property
    def connected(self) -> bool:
        return self.account == CONNECTED

    @property
    def prints(self) -> list[RowDetails]:
        return [self.committed] + ([] if self.duplicate is None else [self.duplicate])

    @property
    def identifier(self) -> str | None:
        """The bank account the posted transaction names in full: the connected record's,
        or the one a full sort code and account number on its committed import row give,
        as commit._counterparty reads it."""
        if self.connected:
            return None if self.names is None else _identifier(self.names)
        printed = self.committed
        if printed.sort_code is None or printed.account_number is None:
            return None
        return f"{printed.sort_code}:{printed.account_number}"


def _weak_evidence(row: Row, prints: list[RowDetails], sort_codes: Mapping[str, str]) -> bool:
    """Whether the row's only evidence is a sort code (eligible_records: no holder, a
    weak_holder): it names no bank account in full, prints no account number alone, and the
    one sort code it prints alone is a registered account's (`sort_codes`)."""
    if row.connected or row.identifier is not None:
        return False
    if any(p.account_number is not None and p.sort_code is None for p in prints):
        return False
    alone = {p.sort_code for p in prints if p.sort_code is not None and p.account_number is None}
    return len(alone) == 1 and alone <= sort_codes.keys()


@dataclass(frozen=True)
class Transfer:
    index: int
    sender: str
    receiver: str
    amount: Decimal
    evidence: Evidence
    synced: Synced | None  # the connected account's record, when it is a side


@dataclass(frozen=True)
class Scenario:
    registered: tuple[str, ...]  # MAIN first
    connected: bool  # whether the connected account takes part
    transfers: tuple[Transfer, ...]
    rows: tuple[Row, ...]  # every row that arrives
    missing: tuple[Row, ...]  # the connected account's records that never do
    action: Action
    selector: int  # which transfer the owner acts on, of those the action applies to
    late: LatePrint
    late_choice: int  # which row the late duplicate is of

    @property
    def complete(self) -> tuple[date, date]:
        """What every account's imports list in full: from 7 days before the first row to 7
        after the last, so no window (1 day before to 5 after, the default) crosses an
        edge."""
        days = [r.day for r in self.rows + self.missing]
        return min(days) - timedelta(days=7), max(days) + timedelta(days=7)

    @property
    def sort_codes(self) -> dict[str, str]:
        """The registered sort codes, and whose they are."""
        accounts = [*self.registered, *([CONNECTED] if self.connected else [])]
        return {_identifier(a).split(":")[0]: a for a in accounts}


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
    connected = draw(st.booleans())
    pairs = [(x, y) for x in registered for y in registered if x != y]
    # A savings account moves money only to and from its main account (design §1).
    pairs += [(MAIN, SAVINGS), (SAVINGS, MAIN)]
    if connected:
        pairs += [pair for x in registered for pair in ((CONNECTED, x), (x, CONNECTED))]
    transfers: list[Transfer] = []
    rows: list[Row] = []
    missing: list[Row] = []
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
        synced: Synced | None = None
        if CONNECTED in (sender, receiver):
            synced = draw(st.sampled_from(("names", "nobody", "missing")))
        transfers.append(Transfer(index, sender, receiver, amount, evidence, synced))
        for key, account, quantity, day, printed, other in (
            (f"t{index}-out", sender, -amount, sent, out, receiver),
            (f"t{index}-in", receiver, amount, received, in_, sender),
        ):
            side = {"transfer": index, "other": other}
            if account != CONNECTED:
                rows.append(Row(key, account, quantity, day, *draw(_copies(printed)), **side))
            elif synced == "missing":
                missing.append(Row(key, account, quantity, day, **side))
            else:
                names = other if synced == "names" else None
                rows.append(Row(key, account, quantity, day, names=names, **side))
    for index in range(draw(st.integers(0, 3))):
        account = draw(st.sampled_from(registered))
        quantity = draw(st.sampled_from(AMOUNTS)) * draw(st.sampled_from((-1, 1)))
        day = START + timedelta(days=draw(st.integers(0, 5)))
        printed = STRANGER if draw(st.booleans()) else None
        rows.append(Row(f"n{index}", account, quantity, day, *draw(_copies(printed))))
    # Noise on the sub-account: naming nobody (so its main account), or part of a bank
    # account only, which names no own account but, a registered sort code, one weakly.
    partial = [STRANGER_SORT_CODE, STRANGER_NUMBER, *(_sort_code(a) for a in registered)]
    for index in range(draw(st.integers(0, 2))):
        quantity = draw(st.sampled_from(AMOUNTS)) * draw(st.sampled_from((-1, 1)))
        day = START + timedelta(days=draw(st.integers(0, 5)))
        printed = draw(st.sampled_from([None, *partial]))
        rows.append(Row(f"s{index}", SAVINGS, quantity, day, *draw(_copies(printed))))
    if not any(r.account == MAIN for r in rows) and any(r.account == SAVINGS for r in rows):
        # The main account's imports list the days its sub-account's rows may name it on
        # (else a placeholder there would be right): a card payment, which never pairs.
        rows.append(Row("n-main", MAIN, CARD, START, bank_transfer=False))
    return Scenario(
        registered,
        connected,
        tuple(transfers),
        tuple(rows),
        tuple(missing),
        action=draw(st.sampled_from(("reject", "unpair"))),
        selector=draw(st.integers(0, 7)),
        late=draw(st.sampled_from(("nothing", "same", "number"))),
        late_choice=draw(st.integers(0, 7)),
    )


type Item = tuple[Literal["row", "duplicate"], str]


@st.composite
def arrivals(draw: st.DrawFn, scenario: Scenario) -> list[Item]:
    """An order the rows arrive in, each in an import (or a sync) of its own: a duplicate
    after the row it duplicates."""
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


def _late_details(row: Row, late: LatePrint) -> RowDetails:
    """What a late duplicate of the row prints (LatePrint)."""
    if late == "nothing":
        return NOTHING
    if late == "same":
        return next((p for p in row.prints if p != NOTHING), NOTHING)
    if row.other is None:
        return STRANGER_NUMBER
    return _number(row.other) if _numbered(row.other) else NOTHING


def _noise_print(row: Row, scenario: Scenario) -> str:
    """What a noise row prints, in words, for event()."""
    printed = next((p for p in row.prints if p != NOTHING), NOTHING)
    if printed == NOTHING:
        return "nothing"
    if printed.sort_code in scenario.sort_codes:
        return "a registered sort code alone"
    return f"a stranger's {'sort code' if printed.sort_code else 'account number'} alone"


def describe(scenario: Scenario) -> str:
    """The scenario, readable, for a failing example's report."""
    connected = f"; {CONNECTED} is connected" if scenario.connected else ""
    lines = [
        f"registered: {', '.join(scenario.registered)}; {SAVINGS} is {MAIN}'s sub-account"
        f"{connected}; imports list {scenario.complete[0]} to {scenario.complete[1]} in full"
    ]
    for t in scenario.transfers:
        synced = "" if t.synced is None else f", {CONNECTED}'s record {t.synced}"
        lines.append(
            f"transfer {t.index}: {t.sender} -> {t.receiver} {t.amount}, {t.evidence}{synced}"
        )
    for r in scenario.rows + scenario.missing:
        if r.connected:
            shows = "never synced" if r in scenario.missing else f"names {r.names or 'nobody'}"
        else:
            duplicate = "" if r.duplicate is None else f", its duplicate {_printed(r.duplicate)}"
            shows = f"prints {_printed(r.committed)}{duplicate}"
            if not r.bank_transfer:
                shows += ", no bank transfer"
        lines.append(f"row {r.key}: {r.account} {r.quantity} on {r.day:%a %d %b}, {shows}")
    lines.append(
        f"then the owner: {scenario.action} #{scenario.selector}; a late duplicate"
        f" #{scenario.late_choice} printing {scenario.late}"
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
    connected_bank: MakeConnected
    connector_tx: ConnectorTx
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


def _own(t: TransferRecord) -> set[UUID]:
    return {x for x in (t.out_transaction_id, t.in_transaction_id) if x is not None}


type State = tuple[list[TransferRecord], dict[str, UUID]]


class Example:
    """One run of a scenario, its rows arriving in a given order, in the test's session."""

    def __init__(self, h: Harness, scenario: Scenario, *, every_heal_twice: bool) -> None:
        self.h = h
        self.session = h.session
        self.scenario = scenario
        self.every_heal_twice = every_heal_twice
        self.rows = {r.key: r for r in scenario.rows}
        self.accounts: dict[str, AccountRecord] = {}
        self.bank: Connected | None = None
        self.posted: dict[str, UUID] = {}  # row key -> transaction id
        self.imported: set[UUID] = set()  # the accounts with an import (listing every day)
        self.late: tuple[str, RowDetails] | None = None  # the late duplicate's row and print
        self.exercised: set[str] = set()  # what the run exercised (for event())

    async def setup(self) -> None:
        for name in self.scenario.registered:
            self.accounts[name] = await self.h.imported_bank(name, identifier=REGISTERED[name])
        self.accounts[SAVINGS] = await self.h.imported_bank(SAVINGS, main=self.accounts[MAIN])
        if self.scenario.connected:
            self.bank = await self.h.connected_bank(CONNECTED)
            account = self.bank.account
            await self.h.run(assign_identifier, account.id, "uk_bank_account", CONNECTED_ID)
            # Its history starts before any window: a record it hasn't synced is awaited.
            await history(self.h.connector_tx, self.bank)
            self.accounts[CONNECTED] = account

    async def _identifier(self, value: str | None) -> UUID | None:
        if value is None:
            return None
        return await self.h.run(get_or_create_identifier, "uk_bank_account", value)

    async def _sync(self, row: Row) -> None:
        assert self.bank is not None
        record = await self.h.connector_tx(
            self.bank,
            str(row.quantity),
            external_id=row.key,
            occurred_at=datetime.combine(row.day, time(), UTC),
            identifier_id=await self._identifier(row.identifier),
        )
        self.posted[row.key] = record.id

    async def arrive(self, item: Item) -> None:
        what, key = item
        row = self.rows[key]
        if what == "duplicate":
            assert row.duplicate is not None
            await self.h.duplicate_row(self.posted[key], row.duplicate)
        elif row.connected:
            await self._sync(row)
        else:
            self.posted[key] = await self.h.statement_row(
                self.accounts[row.account],
                str(row.quantity),
                day=row.day,
                details=row.committed,
                identifier_id=await self._identifier(row.identifier),
                bank_transfer=row.bank_transfer,
                complete=self.scenario.complete,
                key=key,
            )
            self.imported.add(self.accounts[row.account].id)

    async def heal(self) -> None:
        """Heal, then what holds after every heal (_check_heal), and in the first order E2:
        a heal right after matches nothing."""
        await self.h.heal()
        await _check_heal(self.session, self.imported)
        if self.every_heal_twice:
            assert await self.h.heal() == 0  # E2

    async def state(self) -> State:
        transfers = sorted(await list_transfers(self.session), key=lambda t: t.id)
        contras = {
            key: await contra_account(self.session, transaction_id, self._account_of(key))
            for key, transaction_id in self.posted.items()
        }
        return transfers, contras

    def _account_of(self, key: str) -> UUID:
        return self.accounts[self.rows[key].account].id

    def key_of(self, transaction_id: UUID | None) -> str:
        if transaction_id is None:
            return "-"
        [key] = [k for k, posted in self.posted.items() if posted == transaction_id]
        return key

    def _keys(self, t: TransferRecord) -> tuple[str, str]:
        return self.key_of(t.out_transaction_id), self.key_of(t.in_transaction_id)

    def outcome(self, state: State) -> Outcome:
        transfers, contras = state
        names = {a.id: name for name, a in self.accounts.items()}
        names |= {self.h.ids.unassigned: "Unassigned", self.h.ids.in_transit: "In transit"}

        def described(t: TransferRecord) -> str:
            out, in_ = self._keys(t)
            return (
                f"{names[t.from_account_id]} -> {names[t.to_account_id]} {t.amount} {t.status}"
                f" ({t.review_reason or 'no review'}), out {out}, in {in_}"
            )

        return Outcome(
            tuple(sorted(described(t) for t in transfers)),
            {key: names.get(contra, str(contra)) for key, contra in contras.items()},
        )

    async def settle(self) -> State:
        """E2: a heal after the last changes nothing; the state it leaves."""
        before = await self.state()
        assert await self.h.heal() == 0
        assert await self.state() == before
        return before

    async def check_weak(self) -> None:
        """E4, its property checked after every heal: the records it is about are the rows
        printing a registered account's sort code alone, the late duplicate's print
        included."""
        late = self.late
        expected: set[UUID] = set()
        for row in self.scenario.rows:
            prints = row.prints + ([late[1]] if late is not None and late[0] == row.key else [])
            if _weak_evidence(row, prints, self.scenario.sort_codes):
                expected.add(self.posted[row.key])
        assert await _weak_only(self.session) == expected

    async def check_balances(self) -> None:
        """E5: each account's balance is the sum of its rows."""
        balances = await balances_on(self.session, self.scenario.complete[1])
        for name, account in self.accounts.items():
            truth = sum((r.quantity for r in self.scenario.rows if r.account == name), Decimal(0))
            if name == CONNECTED:
                truth += HISTORY
            assert balances.get(account.id, Decimal(0)) == truth, name

    async def check_net_worth(self, transfers: list[TransferRecord]) -> None:
        """E5: net worth is the sum of every true movement, those still to sync that a row
        naming the connected account by account number waits for included; unless a
        transfer of the connected account needs review."""
        connected = self.accounts.get(CONNECTED)
        if connected is not None and any(
            t.review_reason is not None and connected.id in (t.from_account_id, t.to_account_id)
            for t in transfers
        ):
            self.exercised.add("E5 net worth skipped: review on the connected account")
            return
        by_index = {t.index: t for t in self.scenario.transfers}
        awaited = [
            r
            for r in self.scenario.missing
            if r.transfer is not None and by_index[r.transfer].evidence != "sender_prints_sort_code"
        ]
        settings = await load_transfer_settings(self.session)
        for missing in awaited:
            [row] = [r for r in self.scenario.rows if r.transfer == missing.transfer]
            if self._candidate_on_connected(row, settings):
                # The row may pair with another of the connected account's records, and
                # rightly so (§5.3: a number outranks a sort code), so nothing is on its
                # way and net worth can't follow the truth until the record syncs (as E6).
                self.exercised.add("E5 net worth skipped: an awaited row has a candidate")
                return
        truth = sum((r.quantity for r in self.scenario.rows + tuple(awaited)), Decimal(0))
        if connected is not None:
            truth += HISTORY
        assert await net_worth_on(self.session, self.scenario.complete[1]) == truth

    def _candidate_on_connected(self, row: Row, settings: TransferSettings) -> bool:
        """Whether a record of the connected account is a candidate for the other side of
        the row, which names it: going the other way, in the window, and either naming the
        row's account, whatever its amount (pairs._registry_edges: mutually identified ones
        of another amount are candidates too), or naming nobody, of the opposite amount."""
        before, after = settings.window_before, settings.window_after
        for c in self.scenario.rows:
            if not c.connected or (c.quantity < 0) == (row.quantity < 0):
                continue
            if c.names != row.account and (c.names is not None or c.quantity != -row.quantity):
                continue
            sent, received = (row.day, c.day) if row.quantity < 0 else (c.day, row.day)
            if sent - before <= received <= sent + after:
                return True
        return False

    async def check_awaited(self, transfers: list[TransferRecord]) -> None:
        """E6."""
        settings = await load_transfer_settings(self.session)
        for t in self.scenario.transfers:
            if t.synced != "missing" or t.evidence == "sender_prints_sort_code":
                continue
            [row] = [r for r in self.scenario.rows if r.transfer == t.index]
            if self._candidate_on_connected(row, settings):
                continue
            [own] = [x for x in transfers if self.posted[row.key] in _own(x)]
            assert (own.status, own.review_reason, len(_own(own))) == ("awaiting", None, 1), own
            self.exercised.add("E6: a row naming the connected account waits for its record")

    async def act(self) -> TransferRecord | None:
        """The owner's one action (Scenario.action) on a transfer picked by its rows' keys
        among those it applies to; None when it applies to none."""
        transfers = await list_transfers(self.session)
        if self.scenario.action == "reject":
            targets = [t for t in transfers if t.status != "rejected"]
        else:
            targets = [t for t in transfers if t.status == "matched" and len(_own(t)) == 2]
        if not targets:
            self.exercised.add(f"owner: no transfer to {self.scenario.action}")
            return None
        target = sorted(targets, key=self._keys)[self.scenario.selector % len(targets)]
        note(f"owner: {self.scenario.action} {self._keys(target)}")
        await self.h.run(reject if self.scenario.action == "reject" else unpair, target.id)
        self.exercised.add(f"owner: {self.scenario.action} a {target.status} transfer")
        return target

    async def late_duplicate(self, target: TransferRecord | None) -> None:
        """A later file's duplicate of one of the rows: the acted-on transfer's, when it has
        an imported one."""
        imported = sorted(k for k, r in self.rows.items() if not r.connected)
        own = [] if target is None else [self.key_of(x) for x in _own(target)]
        choices = sorted(k for k in own if not self.rows[k].connected) or imported
        key = choices[self.scenario.late_choice % len(choices)]
        details = _late_details(self.rows[key], self.scenario.late)
        note(f"late duplicate of {key}, printing {_printed(details)}")
        await self.h.duplicate_row(self.posted[key], details)
        self.late = (key, details)

    async def resync(self) -> None:
        """E2: every record of the connected account synced again changes nothing."""
        before = await self.state()
        for row in self.scenario.rows:
            if row.connected:
                await self._sync(row)
        assert await self.h.heal() == 0
        assert await self.state() == before

    async def check_decision(self, target: TransferRecord | None, state: State) -> None:
        """E7."""
        if target is None:
            return
        transfers, _ = state
        own = _own(target)
        if self.scenario.action == "reject":
            [kept] = [t for t in transfers if t.id == target.id]
            assert (kept.status, _own(kept)) == ("rejected", own)
            for x in own:
                key = self.key_of(x)
                assert await contra_account(self.session, x, self._account_of(key)) == (
                    self.h.ids.unassigned
                ), key
            assert not any(own & _own(t) for t in transfers if t.id != target.id)
        else:
            assert not any(own <= _own(t) for t in transfers), "unpaired records linked again"


async def _run(
    h: Harness, scenario: Scenario, order: list[Item], *, every_heal_twice: bool
) -> tuple[Outcome, Outcome, set[str]]:
    """One run of the scenario, its rows arriving in `order`, then the owner's action, the
    late duplicate and the re-sync, each followed by heal (and, if `every_heal_twice`,
    another that must match nothing), checking what holds after every heal and E2 to E7;
    returns what E1 compares after the arrivals and at the end, and what the run exercised
    (for event())."""
    x = Example(h, scenario, every_heal_twice=every_heal_twice)
    await x.setup()
    for item in order:
        await x.arrive(item)
        await x.heal()
    arrived = await x.settle()
    transfers, _ = arrived
    assert await placeholders(h.session) == []  # E3
    await x.check_weak()
    await x.check_balances()
    await x.check_net_worth(transfers)
    await x.check_awaited(transfers)
    x.exercised |= await _exercised(h.session, x, transfers)

    target = await x.act()
    await x.heal()
    await x.late_duplicate(target)
    await x.heal()
    await x.resync()
    decided = await x.state()
    assert await placeholders(h.session) == []  # E3
    await x.check_weak()
    await x.check_balances()
    await x.check_decision(target, decided)
    return x.outcome(arrived), x.outcome(decided), x.exercised


async def _exercised(
    session: AsyncSession, x: Example, transfers: list[TransferRecord]
) -> set[str]:
    """What the arrivals exercised, for event()."""
    weak = await _weak_only(session)
    linked = {leg for t in transfers for leg in _own(t)}
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
    return exercised


@given(scenario=scenarios(), data=st.data())
async def test_new_evidence_is_order_independent_idempotent_and_never_anticipates_an_imported_row(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    duplicate_row: DuplicateRow,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    heal: Heal,
    scenario: Scenario,
    data: st.DataObject,
) -> None:
    """E1 to E7."""
    note(describe(scenario))
    h = Harness(
        session,
        run,
        ids,
        imported_bank,
        statement_row,
        duplicate_row,
        connected_bank,
        connector_tx,
        heal,
    )
    first = data.draw(arrivals(scenario), label="first order")
    second = data.draw(arrivals(scenario), label="second order")
    async with isolated_example(session):
        arrived, decided, exercised = await _run(h, scenario, first, every_heal_twice=True)
    async with isolated_example(session):
        arrived_again, decided_again, _ = await _run(h, scenario, second, every_heal_twice=False)
    # E1
    assert arrived_again == arrived
    assert decided_again == decided

    event(f"transfers: {len(scenario.transfers)}")
    for evidence in sorted({t.evidence for t in scenario.transfers}):
        event(f"evidence: {evidence}")
    for synced in sorted({t.synced for t in scenario.transfers if t.synced is not None}):
        event(f"connected account's record: {synced}")
    if any(r.committed == NOTHING and r.duplicate not in (None, NOTHING) for r in scenario.rows):
        event("details printed on a duplicate only")
    for r in scenario.rows:
        if r.account == SAVINGS and r.transfer is None:
            event(f"sub-account noise prints {_noise_print(r, scenario)}")
    event(f"late duplicate prints {scenario.late}")
    for label in sorted(exercised):
        event(label)


@pytest.mark.parametrize("second_sender", ["B", MAIN])
async def test_e5_waits_for_no_record_whose_row_pairs_with_another(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    duplicate_row: DuplicateRow,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    heal: Heal,
    monkeypatch: pytest.MonkeyPatch,
    second_sender: str,
) -> None:
    """The case the drawn scenarios missed (PR #31 review): A pays M 50 printing M's
    account number, and M's record of it never syncs; on the same day another payment of
    50 reaches M printing only M's sort code, and M's record of that one syncs (naming
    nobody, or A in full). A's row rightly pairs with that record (§5.3: a number outranks a
    sort code), so nothing is on its way, and E5 mustn't count the missing record."""
    # Outside @given, hypothesis.note refuses; the run notes what it does.
    monkeypatch.setattr(sys.modules[__name__], "note", lambda _: None)
    names = None if second_sender == "B" else MAIN
    transfers = (
        Transfer(0, MAIN, CONNECTED, Decimal(50), "sender_prints_number", "missing"),
        Transfer(
            1,
            second_sender,
            CONNECTED,
            Decimal(50),
            "sender_prints_sort_code",
            "nobody" if names is None else "names",
        ),
    )
    rows = (
        Row("t0-out", MAIN, Decimal(-50), START, _number(CONNECTED), transfer=0, other=CONNECTED),
        Row(
            "t1-out",
            second_sender,
            Decimal(-50),
            START,
            _sort_code(CONNECTED),
            transfer=1,
            other=CONNECTED,
        ),
        Row("t1-in", CONNECTED, Decimal(50), START, names=names, transfer=1, other=second_sender),
    )
    missing = (Row("t0-in", CONNECTED, Decimal(50), START, transfer=0, other=MAIN),)
    scenario = Scenario(
        (MAIN, "B"),
        True,
        transfers,
        rows,
        missing,
        action="reject",
        selector=0,
        late="nothing",
        late_choice=0,
    )
    h = Harness(
        session,
        run,
        ids,
        imported_bank,
        statement_row,
        duplicate_row,
        connected_bank,
        connector_tx,
        heal,
    )
    order: list[Item] = [("row", "t0-out"), ("row", "t1-out"), ("row", "t1-in")]
    async with isolated_example(session):
        await _run(h, scenario, order, every_heal_twice=True)


# Records naming a bank account (E10's scenarios).
PAYEE = "707070:70707070"  # someone's bank account, registered to no account
PAYEE_ACCOUNTS = ("A", "B", "C", CONNECTED)
# The bank account an account is registered under, when a row names it.
DIRECT = {
    "A": "717171:71717171",
    "B": "727272:72727272",
    "C": "737373:73737373",
    CONNECTED: "747474:74747474",
}


@dataclass(frozen=True)
class PayeeRow:
    """A row: one naming a bank account (`payee`), the one registered to the account
    `names`, or one registered to no account when `names` is None; or one naming nobody,
    its import row printing a sort code alone or nothing."""

    key: str
    account: str
    quantity: Decimal  # signed
    day: date
    payee: bool = False
    names: str | None = None
    sort_code: str | None = None

    @property
    def connected(self) -> bool:
        return self.account == CONNECTED


@dataclass(frozen=True)
class PayeeScenario:
    targets: tuple[str, ...]  # the accounts registered under their DIRECT bank account
    rows: tuple[PayeeRow, ...]  # in the order they arrive

    @property
    def complete(self) -> tuple[date, date]:
        days = [r.day for r in self.rows]
        return min(days) - timedelta(days=7), max(days) + timedelta(days=7)


@st.composite
def payee_scenarios(draw: st.DrawFn) -> PayeeScenario:
    targets = draw(st.lists(st.sampled_from(PAYEE_ACCOUNTS), min_size=1, max_size=4, unique=True))
    rows: list[PayeeRow] = []
    for index in range(draw(st.integers(1, 4))):
        account = draw(st.sampled_from(PAYEE_ACCOUNTS))
        quantity = draw(st.sampled_from(AMOUNTS)) * draw(st.sampled_from((-1, 1)))
        day = START + timedelta(days=draw(st.integers(0, 3)))
        names = draw(st.sampled_from((*targets, None)))
        rows.append(PayeeRow(f"p{index}", account, quantity, day, True, names))
        if draw(st.integers(0, 3)) == 0:
            continue  # no other side
        # Its other side, naming nobody: on the account it names, else elsewhere.
        others = [a for a in PAYEE_ACCOUNTS if a != account]
        other = names if names in others else draw(st.sampled_from(others))
        # An imported one may print the connected account's sort code alone (weak_tier's).
        weak = account == CONNECTED and other != CONNECTED and draw(st.booleans())
        rows.append(
            PayeeRow(
                f"p{index}-other",
                other,
                -quantity,
                day + timedelta(days=draw(st.sampled_from((0, 0, 1, -1)))),
                sort_code=_identifier(CONNECTED).split(":")[0] if weak else None,
            )
        )
    for index in range(draw(st.integers(0, 2))):
        account = draw(st.sampled_from(PAYEE_ACCOUNTS))
        quantity = draw(st.sampled_from(AMOUNTS)) * draw(st.sampled_from((-1, 1)))
        day = START + timedelta(days=draw(st.integers(0, 5)))
        rows.append(PayeeRow(f"n{index}", account, quantity, day))
    return PayeeScenario(tuple(targets), tuple(draw(st.permutations(rows))))


def describe_payee(scenario: PayeeScenario) -> str:
    lines = [f"registered under their bank account: {', '.join(scenario.targets)}"]
    for r in scenario.rows:
        if r.payee:
            shows = f"names {r.names or 'a bank account registered to no account'}"
        else:
            shows = f"names nobody, prints {r.sort_code or 'nothing'}"
        lines.append(f"row {r.key}: {r.account} {r.quantity} on {r.day:%a %d %b}, {shows}")
    return "\n".join(lines)


# Accounts funded by bank transfer (account type catalog design §8.2; E10).
# A typed bank account here stands in for a Trading 212 one: the engine reads only type_key.
FUNDED_KEY = min(keys_funded_by("bank_transfer"))


@dataclass(frozen=True)
class FundingScenario:
    payee: PayeeScenario
    unmarked: frozenset[str]  # the rows (keys) not marked a bank transfer
    funded: frozenset[str]  # the accounts given a type funded by bank transfer

    def affected(self) -> frozenset[str]:
        """The rows the funding arm may apply to: unmarked, naming nobody (no payee, no
        sort code printed) and on a funded account."""
        return frozenset(
            r.key
            for r in self.payee.rows
            if r.key in self.unmarked
            and not r.payee
            and r.sort_code is None
            and r.account in self.funded
        )


@st.composite
def funding_scenarios(draw: st.DrawFn) -> FundingScenario:
    payee = draw(payee_scenarios())
    # Each row and account drawn on its own, so a row is as likely unmarked on a funded
    # account as not.
    unmarked = {r.key for r in payee.rows if draw(st.booleans())}
    funded = {a for a in PAYEE_ACCOUNTS if draw(st.booleans())}
    funded = funded or {draw(st.sampled_from(PAYEE_ACCOUNTS))}
    return FundingScenario(payee, frozenset(unmarked), frozenset(funded))


type Edges = dict[tuple[str, str], int]


async def _funding_run(h: Harness, scenario: FundingScenario, funded: frozenset[str]) -> Edges:
    """One run of the payee scenario, its `unmarked` rows not marked bank transfers, and the
    `funded` accounts typed FUNDED_KEY, heal after every row (checking M4's invariants);
    returns the pair relation's same-amount edges (both ways round), by row keys."""
    payee = scenario.payee
    accounts = {name: await h.imported_bank(name) for name in PAYEE_ACCOUNTS[:3]}
    bank = await h.connected_bank(CONNECTED)
    await h.run(assign_identifier, bank.account.id, "uk_bank_account", CONNECTED_ID)
    accounts[CONNECTED] = bank.account
    for name in sorted(funded):
        await h.run(set_type_key, accounts[name].id, FUNDED_KEY)
    directly = {
        name: (
            await h.run(assign_identifier, accounts[name].id, "uk_bank_account", DIRECT[name])
        ).id
        for name in payee.targets
    }
    payee_id = await h.run(get_or_create_identifier, "uk_bank_account", PAYEE)
    posted: dict[str, UUID] = {}
    for row in payee.rows:
        value, identifier = None, None
        if row.payee and row.names is not None:
            value, identifier = DIRECT[row.names], directly[row.names]
        elif row.payee:
            value, identifier = PAYEE, payee_id
        bank_transfer = row.key not in scenario.unmarked
        if row.connected:
            record = await h.connector_tx(
                bank,
                str(row.quantity),
                external_id=row.key,
                occurred_at=datetime.combine(row.day, time(), UTC),
                identifier_id=identifier,
                bank_transfer=bank_transfer,
            )
            posted[row.key] = record.id
        else:
            sort_code, number = value.split(":") if value else (row.sort_code, None)
            posted[row.key] = await h.statement_row(
                accounts[row.account],
                str(row.quantity),
                day=row.day,
                details=RowDetails(sort_code=sort_code, account_number=number),
                identifier_id=identifier,
                bank_transfer=bank_transfer,
                complete=payee.complete,
                key=row.key,
            )
        await h.heal()
        await assert_consistent(h.session)
    assert await h.heal() == 0

    system = await system_account_ids(h.session)
    relation = await pair_relation(h.session, system, await load_transfer_settings(h.session))
    keys = {transaction_id: key for key, transaction_id in posted.items()}
    return {
        (keys[r], keys[c]): tier for r, found in relation.tiers.items() for c, tier in found.items()
    }


@given(scenario=funding_scenarios())
async def test_funding_changes_only_the_pairs_of_unmarked_deposits_on_funded_accounts(
    session: AsyncSession,
    run: Run,
    ids: SystemIds,
    imported_bank: ImportedBank,
    statement_row: StatementRow,
    duplicate_row: DuplicateRow,
    connected_bank: MakeConnected,
    connector_tx: ConnectorTx,
    heal: Heal,
    scenario: FundingScenario,
) -> None:
    """E10: marking a set of accounts as funded by bank transfer never changes any pair
    (an edge of the pair relation, with its tier) that involves no unmarked record naming
    nobody on those accounts; each pair it adds is ONE_SIDED, between such a record and
    one naming its account. It compares the same-currency tiers (PairRelation.tiers): card
    and cross-currency edges are outside it, and the scenarios have neither. (A transfer may
    still change: a deposit that gains a pair can take an identified record's only candidate
    from it.)"""
    note(describe_payee(scenario.payee))
    note(f"unmarked rows {sorted(scenario.unmarked)}, funded accounts {sorted(scenario.funded)}")
    h = Harness(
        session,
        run,
        ids,
        imported_bank,
        statement_row,
        duplicate_row,
        connected_bank,
        connector_tx,
        heal,
    )
    async with isolated_example(session):
        plain = await _funding_run(h, scenario, frozenset())
    async with isolated_example(session):
        funded = await _funding_run(h, scenario, scenario.funded)
    affected = scenario.affected()
    assert {pair: tier for pair, tier in funded.items() if not affected & set(pair)} == plain
    rows = {r.key: r for r in scenario.payee.rows}
    added = {pair: tier for pair, tier in funded.items() if affected & set(pair)}
    for (r, c), tier in added.items():
        deposit, other = (rows[r], rows[c]) if r in affected else (rows[c], rows[r])
        assert tier == ONE_SIDED, (r, c, tier)
        assert other.names == deposit.account, (r, c)

    event(f"funded accounts: {len(scenario.funded)}")
    event(f"unmarked rows: {len(scenario.unmarked)}")
    if added:
        event("an unmarked deposit on a funded account gained a pair")
    elif affected:
        event("an unmarked deposit on a funded account, with no pair")
