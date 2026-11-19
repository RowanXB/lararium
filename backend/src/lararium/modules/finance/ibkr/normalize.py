"""IBKR raw records into the ledger.

Deterministic from its inputs and versioned (NORMALIZER_VERSION). So far it maps accounts
(design 5.5): one finance account per IBKR account, found or created from its Account
Information row, closed on its dateClosed, given the tax wrapper its customerType says, and
registered under its IBKR account id. Later parts post the rest of the statement.

Account Information holds personal data (name, address, e-mail, date of birth): it is stored
in ibkr_accounts and never logged or put in a warning.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.modules.finance.accounts import (
    SourceDetails,
    TaxWrapper,
    close_account,
    create_account,
    get_account,
    set_source_details,
)
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.ibkr import tables
from lararium.modules.finance.ibkr.fields import SECTIONS
from lararium.modules.finance.identifiers import ensure_identifier, get_or_create_identifier
from lararium.modules.finance.instruments import get_currency

logger = logging.getLogger(__name__)

NORMALIZER_VERSION = "ibkr-norm-1"
ACCOUNT_MAP_KEY = "account_map"
INSTITUTION = "Interactive Brokers"
_SECTION = "AccountInformation"
_ACCOUNT_RECORD_TYPE = SECTIONS[_SECTION].record_type


@dataclass
class NormalizeReport:
    """What a normalize run changed."""

    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0


def tax_wrapper(customer_type: str) -> TaxWrapper | None:
    """The tax wrapper a customerType says (design 5.5); None leaves the account's as it is."""
    if "Individual Savings Account" in customer_type:
        return "isa"
    if customer_type == "Individual":
        return "gia"
    return None


async def _in_short_transaction[T](
    ctx: SyncContext, work: Callable[[CommandContext], Awaitable[T]]
) -> T:
    """Run work in a transaction of its own, committed at once: rows other connections share
    are created under a lock that must not be held until this sync, which may take minutes,
    commits."""
    async with (
        ctx.session_factory() as session,
        command_context(session, ctx.command.actor, "finance.shared_row", {}) as cmd,
    ):
        return await work(cmd)


def _latest_per_account(records: Sequence[RawRecord]) -> dict[str, dict[str, str]]:
    """The newest Account Information attributes of each IBKR account: statements repeat the
    section for every day, and the latest one says how the account is now."""
    newest: dict[str, tuple[str, str, dict[str, str]]] = {}
    for record in records:
        if record.record_type != _ACCOUNT_RECORD_TYPE or not isinstance(record.content, dict):
            continue
        account = str(record.content["account"])
        period = record.content["period"]
        attributes = record.content["attributes"]
        assert isinstance(period, dict) and isinstance(attributes, dict)
        order = (str(period["to"]), str(record.id))
        if account not in newest or order > newest[account][:2]:
            newest[account] = (*order, {k: str(v) for k, v in attributes.items()})
    return {account: attributes for account, (_, _, attributes) in newest.items()}


class _Accounts:
    def __init__(self, ctx: SyncContext) -> None:
        self.ctx = ctx
        self.command = ctx.command
        stored = ctx.connection.config.get(ACCOUNT_MAP_KEY)
        self.account_map: dict[str, JSONValue] = dict(stored) if isinstance(stored, dict) else {}
        self.institution_id: UUID | None = None
        self.report = NormalizeReport()

    async def institution(self) -> UUID:
        if self.institution_id is None:
            self.institution_id = await _in_short_transaction(
                self.ctx, lambda cmd: get_or_create_organization(cmd, INSTITUTION)
            )
        return self.institution_id

    async def existing(self, ibkr_id: str) -> UUID | None:
        mapped = self.account_map.get(ibkr_id)
        if not isinstance(mapped, str):
            return None
        try:
            await get_account(self.command.session, UUID(mapped))
        except AccountNotFoundError:
            return None
        return UUID(mapped)

    async def account(self, ibkr_id: str, attributes: dict[str, str]) -> None:
        opened = tables.parse_date(attributes["dateOpened"], f"{_SECTION}.dateOpened")
        closed = (
            tables.parse_date(attributes["dateClosed"], f"{_SECTION}.dateClosed")
            if attributes.get("dateClosed", "").strip()
            else None
        )
        changed = False
        account_id = await self.existing(ibkr_id)
        created_now = account_id is None
        if account_id is None:
            created = await create_account(
                self.command,
                name=f"IBKR ···{ibkr_id[-4:]}",
                ledger_type="asset",
                subtype="broker",
                balance_mode="transactions",
                institution_entity_id=await self.institution(),
                opened_on=opened,
                source_connection_id=self.ctx.connection.id,
            )
            account_id = created.id
            self.account_map[ibkr_id] = str(account_id)
        if closed is not None:
            changed |= await self.close(account_id, closed)
        wrapper = tax_wrapper(attributes.get("customerType", ""))
        changed |= await set_source_details(
            self.command, account_id, SourceDetails(tax_wrapper=wrapper)
        )
        # The row exists before ensure_identifier runs, so it takes no lock in this sync's
        # transaction (identifiers._get_or_create).
        await _in_short_transaction(
            self.ctx, lambda cmd: get_or_create_identifier(cmd, "ibkr_account", ibkr_id)
        )
        changed |= await ensure_identifier(self.command, account_id, "ibkr_account", ibkr_id)
        changed |= await self.store(account_id, ibkr_id, attributes)
        if created_now:
            self.report.accounts_created += 1
        elif changed:
            self.report.accounts_updated += 1
        else:
            self.report.accounts_unchanged += 1

    async def close(self, account_id: UUID, closed_on: date) -> bool:
        if (await get_account(self.command.session, account_id)).status != "open":
            return False
        await close_account(self.command, account_id, closed_on)
        return True

    async def store(self, account_id: UUID, ibkr_id: str, attributes: dict[str, str]) -> bool:
        session = self.command.session
        currency = await get_currency(session, attributes["currency"])
        values = await tables.typed_values(session, "ibkr_accounts", _SECTION, attributes)
        values |= {"ibkr_account_id": ibkr_id, "base_instrument_id": currency.id}
        return await tables.upsert_accounts(self.command, {"account_id": account_id}, values)


async def normalize(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], flagged: frozenset[str] = frozenset()
) -> NormalizeReport:
    del flagged
    records = await get_raw_records(ctx.command.session, raw_record_ids)
    accounts = _Accounts(ctx)
    for ibkr_id, attributes in sorted(_latest_per_account(records).items()):
        await accounts.account(ibkr_id, attributes)
    if accounts.account_map != ctx.connection.config.get(ACCOUNT_MAP_KEY):
        await update_config(ctx.command, ctx.connection.id, {ACCOUNT_MAP_KEY: accounts.account_map})
    return accounts.report
