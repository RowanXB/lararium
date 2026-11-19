"""What one normalize run of an IBKR connection shares: its rows, accounts, instruments and
system accounts, and what it reports.

Flags name sections, fields and asset categories only, never a row's values.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from lararium.core.commands import CommandContext, command_context
from lararium.core.connectors import SyncContext
from lararium.modules.finance.errors import UnknownCurrencyError
from lararium.modules.finance.ibkr import tables
from lararium.modules.finance.ibkr.instruments import listing
from lararium.modules.finance.ibkr.parser import FlexDataError, Row
from lararium.modules.finance.instruments import InstrumentRecord, get_currency
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)

_CATEGORY = re.compile(r"[A-Z]{1,12}")


@dataclass
class NormalizeReport:
    """What a normalize run changed, and what it could not map (for warnings, Task 6)."""

    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0
    # Ledger transactions (trades, cash movements, corporate actions, transfers, openings).
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # "<Section>.<attribute>", "<Section>.assetCategory=<category>" and the like.
    flagged: set[str] = field(default_factory=set[str])
    mismatches: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Item:
    """One statement row, as its raw record holds it."""

    record_id: UUID
    record_type: str
    section: str
    account: str
    period_from: date
    period_to: date
    attributes: dict[str, str]
    fetched_at: datetime

    def row(self) -> Row:
        return Row(self.section, self.account, self.period_from, self.period_to, self.attributes)

    def text(self, name: str) -> str:
        return self.attributes.get(name, "").strip()

    def number(self, name: str) -> Decimal:
        """The attribute as a number; an empty or missing one is 0."""
        value = self.text(name)
        return tables.parse_decimal(value, f"{self.section}.{name}") if value else Decimal(0)

    def day(self, name: str) -> date:
        return tables.parse_date(self.text(name), f"{self.section}.{name}")

    def optional_day(self, name: str) -> date | None:
        return self.day(name) if self.text(name) else None

    def moment(self, name: str = "dateTime") -> datetime:
        return tables.parse_moment(self.text(name), f"{self.section}.{name}")


def zone_name(moment: datetime) -> str | None:
    zone = moment.tzinfo
    return zone.key if isinstance(zone, ZoneInfo) else None


def category_flag(item: Item) -> str:
    category = item.text("assetCategory")
    if _CATEGORY.fullmatch(category) is None:
        return f"{item.section}.assetCategory"
    return f"{item.section}.assetCategory={category}"


class Run:
    def __init__(
        self, ctx: SyncContext, accounts: dict[str, UUID], report: NormalizeReport
    ) -> None:
        self.ctx = ctx
        self.command = ctx.command
        self.session = ctx.command.session
        self.connection_id = ctx.connection.id
        # IBKR account id to finance account.
        self.accounts = accounts
        self.report = report
        self._instruments: dict[str, UUID | None] = {}
        self._currencies: dict[str, InstrumentRecord | None] = {}
        self._shared: dict[tuple[str, ...], UUID] = {}

    def account_id(self, item: Item) -> UUID:
        found = self.accounts.get(item.account)
        if found is None:
            raise FlexDataError(f"{item.section}.accountId has no AccountInformation")
        return found

    def source(self, external_id: str) -> tuple[str, str]:
        """The transaction's stable reference for the owner's transfer decisions."""
        return ("connection", f"{self.connection_id}:{external_id}")

    def count(self, outcome: Literal["created", "updated", "unchanged"]) -> None:
        setattr(self.report, outcome, getattr(self.report, outcome) + 1)

    def flag(self, name: str) -> None:
        self.report.flagged.add(name)

    async def currency(self, code: str) -> InstrumentRecord:
        found = await self.known_currency(code)
        if found is None:
            raise UnknownCurrencyError(code)
        return found

    async def known_currency(self, code: str) -> InstrumentRecord | None:
        """The currency, or None for one the ledger does not have."""
        if code not in self._currencies:
            try:
                self._currencies[code] = await get_currency(self.session, code)
            except UnknownCurrencyError:
                self._currencies[code] = None
        return self._currencies[code]

    async def instrument(self, item: Item, *, flag: bool = True) -> UUID | None:
        """The listing of the row's security, found or created once per conid in a run;
        None for a category M5 does not handle, which is flagged unless `flag` is False."""
        conid = item.text("conid")
        if conid not in self._instruments:
            self._instruments[conid] = await listing(self.command, item.row())
        found = self._instruments[conid]
        if found is None and flag:
            self.flag(category_flag(item))
        return found

    async def shared(
        self, key: tuple[str, ...], work: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        """A row other connections share, found or created in a transaction of its own and
        committed at once: its lock must not be held until this sync commits."""
        if key not in self._shared:
            async with (
                self.ctx.session_factory() as session,
                command_context(session, self.command.actor, "finance.shared_row", {}) as cmd,
            ):
                self._shared[key] = await work(cmd)
        return self._shared[key]

    async def system_account(self, key: SystemAccountKey) -> UUID:
        return await self.shared(
            ("system_account", key), lambda cmd: get_or_create_system_account(cmd, key)
        )
