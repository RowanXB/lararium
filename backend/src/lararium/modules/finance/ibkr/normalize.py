"""IBKR raw records into the ledger.

Deterministic from its inputs and versioned (NORMALIZER_VERSION, stored in the connection's
config): when the stored version differs, the run replays every raw record the connection
holds (the latest copy of each row), so what a new version stores is filled in for the
history an older one saw too.

In order (design 5.5-5.10): accounts (one finance account per IBKR account, closed on its
dateClosed, its tax wrapper from customerType, registered under its IBKR account id);
instruments; FX rates; prices; Cash Reports, NAV and the other levels of Trades; trades, cash
transactions, corporate actions and transfers (records.py); opening adjustments, for the
account's earliest window only; reported snapshots (reports.py).

Account Information holds personal data (name, address, e-mail, date of birth): it is stored
in ibkr_accounts and never logged or put in a warning. Errors and flags name sections, fields,
categories, IBKR's transaction ids and an account's last four digits, never a row's values.
"""

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from decimal import MAX_PREC, localcontext
from uuid import UUID

from sqlalchemy import func, select

from lararium.core import models as core_models
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import get_connection, update_config
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
from lararium.modules.finance.ibkr import reconcile, records, reports, tables
from lararium.modules.finance.ibkr.fields import SECTIONS
from lararium.modules.finance.ibkr.parser import FlexDataError
from lararium.modules.finance.ibkr.state import Item, NormalizeReport, Run
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)
from lararium.modules.finance.instruments import get_currency

logger = logging.getLogger(__name__)

# 2: the NAV long/short parts have columns; 3: so do the Cash Report's segment parts. Each
# bump replays the stored rows into the new columns.
NORMALIZER_VERSION = "ibkr-norm-3"
# The version that last normalized the connection, in its config (Decision 8).
NORMALIZER_VERSION_KEY = "normalizer_version"
ACCOUNT_MAP_KEY = "account_map"
INSTITUTION = "Interactive Brokers"
_SECTION = "AccountInformation"
_SECTION_OF = {spec.record_type: section for section, spec in SECTIONS.items()}


def _type(section: str) -> str:
    return SECTIONS[section].record_type


_ACCOUNT = _type(_SECTION)
_OPEN_POSITION = _type("OpenPosition")
_POSITION_LOT = _type("PositionLot")
_PRIOR_POSITION = _type("PriorPeriodPosition")
_CASH_REPORT = _type("CashReportCurrency")
_TRADE = _type("Trade")
_TRADE_DETAIL = _type("TradeDetail")
_CASH_TRANSACTION = _type("CashTransaction")
_CONVERSION_RATE = _type("ConversionRate")
_CORPORATE_ACTION = _type("CorporateAction")
_TRANSFER = _type("Transfer")
_NAV = _type("EquitySummaryByReportDateInBase")
# The record types whose rows may name a security, in the order their instruments are made.
_WITH_INSTRUMENTS = (
    _OPEN_POSITION,
    _POSITION_LOT,
    _PRIOR_POSITION,
    _TRADE,
    _TRADE_DETAIL,
    _CORPORATE_ACTION,
    _TRANSFER,
    _CASH_TRANSACTION,
)
# Where a row's security is optional: no conid means none, and no flag.
_OPTIONAL_INSTRUMENT = frozenset({_TRADE_DETAIL, _CASH_TRANSACTION})


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


def _latest_per_account(items: Sequence[Item]) -> dict[str, dict[str, str]]:
    """The newest Account Information attributes of each IBKR account: statements repeat the
    section for every day, and the latest one says how the account is now."""
    newest: dict[str, tuple[str, str, dict[str, str]]] = {}
    for item in items:
        if item.record_type != _ACCOUNT:
            continue
        order = (item.period_to.isoformat(), str(item.record_id))
        if item.account not in newest or order > newest[item.account][:2]:
            newest[item.account] = (*order, item.attributes)
    return {account: attributes for account, (_, _, attributes) in newest.items()}


def _item(record: RawRecord) -> Item | None:
    section = _SECTION_OF.get(record.record_type)
    content = record.content
    if section is None or not isinstance(content, dict):
        return None
    period, attributes = content["period"], content["attributes"]
    assert isinstance(period, dict) and isinstance(attributes, dict)
    return Item(
        record_id=record.id,
        record_type=record.record_type,
        section=section,
        account=str(content["account"]),
        period_from=date.fromisoformat(str(period["from"])),
        period_to=date.fromisoformat(str(period["to"])),
        attributes={k: str(v) for k, v in attributes.items()},
        fetched_at=record.fetched_at,
    )


def _key(item: Item) -> tuple[str, ...]:
    """What makes a row the same row in another statement: its ledger key for the
    transactional sections, else the account, security and date it reports on."""
    kind, text = item.record_type, item.text
    if kind in records.PREFIXES:
        return kind, records.external_id(item)
    if kind == _ACCOUNT:
        # Every copy is kept: _latest_per_account picks the one with the latest period.
        return kind, item.account, str(item.record_id)
    if kind in (_OPEN_POSITION, _POSITION_LOT):
        lot = tables.lot_key(item.attributes) if reports.is_lot(item) else ""
        return kind, item.account, text("conid"), text("reportDate"), text("levelOfDetail"), lot
    if kind == _PRIOR_POSITION:
        return kind, item.account, text("conid"), text("date")
    if kind == _CASH_REPORT:
        return kind, item.account, text("currency"), text("fromDate"), text("toDate")
    if kind == _CONVERSION_RATE:
        return kind, text("reportDate"), text("fromCurrency"), text("toCurrency")
    if kind == _NAV:
        return kind, item.account, text("reportDate")
    if kind == _TRADE_DETAIL:
        return kind, item.account, text("levelOfDetail"), tables.trade_detail_key(item.attributes)
    return kind, str(item.record_id)


async def _connection_records(ctx: SyncContext) -> list[RawRecord]:
    """Every raw record of the connection, oldest fetch first."""
    r = core_models.RawRecord
    ids = await ctx.command.session.scalars(
        select(r.id).where(r.connection_id == ctx.connection.id).order_by(r.fetched_at, r.id)
    )
    return await get_raw_records(ctx.command.session, list(ids))


async def _items(ctx: SyncContext, raw_record_ids: Sequence[UUID], replay: bool) -> list[Item]:
    """This run's rows, one per key; with `replay`, also the latest copy of every other row
    the connection holds.

    The same transaction id with different attributes in one run fails it, naming the
    section, the transaction id and the account's last four digits (Decision 9): a row
    repeated unchanged (by overlapping windows, under another period) is one row.
    """
    given = [
        item
        for item in map(_item, await get_raw_records(ctx.command.session, raw_record_ids))
        if item is not None
    ]
    chosen: dict[tuple[str, ...], Item] = {}
    for item in sorted(given, key=lambda i: (i.fetched_at, i.record_id)):
        key = _key(item)
        held = chosen.get(key)
        if held is not None and key[0] in records.PREFIXES and held.attributes != item.attributes:
            raise FlexDataError(f"{item.section} has two different rows for {records.shown(item)}")
        chosen[key] = item
    if replay:
        given_keys = set(chosen)
        for record in await _connection_records(ctx):
            item = _item(record)
            if item is not None and (key := _key(item)) not in given_keys:
                chosen[key] = item  # oldest first, so the latest copy stays
    return list(chosen.values())


class _Accounts:
    def __init__(self, ctx: SyncContext, config: dict[str, JSONValue]) -> None:
        self.ctx = ctx
        self.command = ctx.command
        stored = config.get(ACCOUNT_MAP_KEY)
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
        values |= {
            "ibkr_account_id": normalize_identifier("ibkr_account", ibkr_id),
            "base_instrument_id": currency.id,
        }
        return await tables.upsert_accounts(self.command, {"account_id": account_id}, values)


async def _earliest_history(ctx: SyncContext) -> dict[str, date]:
    """The first day of the earliest statement the connection holds for each account, this
    run's included: a run holding it sets the account's opening (design 5.7). That may be a
    later run than the first sync, when an upload reaches further back."""
    r = core_models.RawRecord
    account = r.content["account"].astext
    rows = await ctx.command.session.execute(
        select(account, func.min(r.content["period"]["from"].astext))
        .where(r.connection_id == ctx.connection.id, r.content.has_key("period"))
        .group_by(account)
    )
    return {ibkr_id: date.fromisoformat(day) for ibkr_id, day in rows if ibkr_id and day}


async def normalize(
    ctx: SyncContext,
    raw_record_ids: Sequence[UUID],
    flagged: frozenset[str] = frozenset(),
    sections: frozenset[tuple[str, str]] = frozenset(),
) -> NormalizeReport:
    """Apply raw records to the ledger. `flagged` (the parser's flags) is reported with this
    run's own (unsupported categories, rows it could not post). `sections` are the
    (accountId, section) pairs the ingested statements list (IngestResult.sections): an
    opening counts a window's positions only when its statements list Open Positions.

    Its arithmetic on amounts is exact, as the ledger's balance check is: the default context
    rounds to 28 digits, which could leave postings that do not balance."""
    with localcontext(prec=MAX_PREC):
        return await _normalize(ctx, raw_record_ids, flagged, sections)


async def _normalize(
    ctx: SyncContext,
    raw_record_ids: Sequence[UUID],
    flagged: frozenset[str],
    sections: frozenset[tuple[str, str]],
) -> NormalizeReport:
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    replay = connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
    items = await _items(ctx, list(dict.fromkeys(raw_record_ids)), replay)
    accounts = _Accounts(ctx, connection.config)
    latest = _latest_per_account(items)
    for ibkr_id, attributes in sorted(latest.items()):
        await accounts.account(ibkr_id, attributes)
    mapped = {k: UUID(v) for k, v in accounts.account_map.items() if isinstance(v, str)}
    run = Run(ctx, mapped, accounts.report)
    run.report.flagged |= flagged

    by_type: defaultdict[str, list[Item]] = defaultdict(list)
    for item in sorted(
        items, key=lambda i: (i.text("reportDate"), i.period_from, i.fetched_at, i.record_id)
    ):
        by_type[item.record_type].append(item)
    for kind in _WITH_INSTRUMENTS:
        for item in by_type[kind]:
            category = item.text("assetCategory")
            optional = kind in _OPTIONAL_INSTRUMENT and not (category and item.text("conid"))
            if category != "CASH" and not optional:
                await run.instrument(item, flag=False)
    await reports.fx_rates(run, by_type[_CONVERSION_RATE])
    positions = by_type[_OPEN_POSITION] + by_type[_POSITION_LOT]
    await reports.prices(run, by_type[_PRIOR_POSITION], positions)
    await reports.cash_reports(run, by_type[_CASH_REPORT])
    await reports.nav(run, by_type[_NAV])
    await reports.trade_details(run, by_type[_TRADE_DETAIL])

    for item in sorted(by_type[_TRADE], key=records.order):
        await records.trade(run, item)
    for item in sorted(by_type[_CASH_TRANSACTION], key=records.order):
        await records.cash_transaction(run, item)
    for item in sorted(by_type[_CORPORATE_ACTION], key=records.order):
        await records.corporate_action(run, item)
    for item in sorted(by_type[_TRANSFER], key=records.order):
        await records.transfer(run, item)
    opened = {
        ibkr_id: tables.parse_date(attributes["dateOpened"], f"{_SECTION}.dateOpened")
        for ibkr_id, attributes in latest.items()
    }
    await records.openings(
        run,
        items,
        opened,
        await _earliest_history(ctx),
        cash_report=_CASH_REPORT,
        open_position=_OPEN_POSITION,
        is_summary=lambda item: not reports.is_lot(item),
        sections=sections,
    )
    await reports.snapshots(run, positions, by_type[_CASH_REPORT])

    # Reconciliation (design 5.11): every account at its latest reported day, and the end
    # of each window this run ingested.
    given = set(raw_record_ids)
    windows = {
        (item.account, item.period_to)
        for item in items
        if item.record_type == _ACCOUNT and item.record_id in given
    }
    checked = await reconcile.check(ctx.command.session, mapped, windows, sections)
    run.report.mismatches = [m.warning for m in checked.mismatches]
    await reconcile.publish(ctx, connection, checked, run.report.flagged)

    # Only this normaliser's keys: others (the token's reminder) may have changed meanwhile.
    values: dict[str, JSONValue] = {
        ACCOUNT_MAP_KEY: accounts.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
    }
    old = [key for key in reconcile.OLD_KEYS if key in connection.config]
    if old or any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values, remove=old)
    return run.report
