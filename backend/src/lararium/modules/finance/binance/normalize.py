"""Binance raw records into the typed tables and the ledger (design 5.3, 5.7, 5.13).

A run applies every history raw record after `config["normalized_through"]` (raw record ids
are UUIDv7, so in the order they were stored): each is checked against the catalogue (a field
or value it does not list becomes a flag), its typed row upserted, and its label's handler
run. A row that changed (a withdrawal that failed) is a new raw record, so it is applied again
and updates its typed row and transactions in place. Then a movement two endpoints list is
posted once (rule 2), rewards are linked to the dividends that paid them (rule 3), travel-rule
rows to the deposits and withdrawals they describe, a flexible loan's interest derived from the
reports (`loans.set_interest`), and the new flags published.

The run writes in a transaction of its own: making a wallet's account and setting the
connection's config lock its row, which a later step of the sync that walks (in short
transactions of its own) must be able to write. What it touched is queued for reconciliation
in that transaction (`reconcile.queue_touched`), and the sync's clear reads are recorded as
reports of the accounts its records made (`balances.record_reads`): an account never exists
without the report times of the reads made before it. Its arithmetic is exact, as the ledger's
balance check is.
"""

from collections.abc import Iterable, Mapping
from decimal import MAX_PREC, localcontext
from uuid import UUID

from sqlalchemy import select, text

from lararium.core import models as core_models
from lararium.core.commands import command_context
from lararium.core.connections import get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.modules.finance.binance import layout, notices, tables
from lararium.modules.finance.binance.balances import Read, record_reads
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import record_type
from lararium.modules.finance.binance.parse import (
    ApiValue,
    Row,
    as_api,
    parse,
    unknown_flags,
)
from lararium.modules.finance.binance.reconcile import queue_touched
from lararium.modules.finance.binance.records import (
    BinanceRecordError,
    Current,
    Handler,
    NormalizeReport,
    Run,
    capital,
    earn,
    fiat,
    futures,
    loans,
    margin,
    options,
    post,
    remove,
    spot,
    staking,
    structured,
    transfers,
)

__all__ = [
    "HANDLERS",
    "MOVEMENT_LISTINGS",
    "NORMALIZER_VERSION",
    "BinanceRecordError",
    "Handler",
    "NormalizeReport",
    "Run",
    "link_payouts",
    "normalize",
    "post",
    "publish_flags",
    "regenerate",
    "remove",
]

NORMALIZER_VERSION = "binance-norm-2"
VERSION_KEY = "normalizer_version"
NORMALIZED_THROUGH_KEY = "normalized_through"
FLAGS_KEY = notices.FLAGS_KEY
FLAG_PREFIX = notices.FLAG_PREFIX
# Rule 3: a reward and the dividend that paid it are at most this far apart.
PAYOUT_WINDOW_HOURS = 26

# A label with no handler is typed only.
HANDLERS: dict[str, Handler] = {
    capital.DEPOSITS: capital.deposit,
    capital.WITHDRAWALS: capital.withdrawal,
    spot.TRADES: spot.trade,
    spot.CONVERT: spot.convert,
    spot.DIVIDENDS: spot.dividend,
    earn.SUBSCRIPTIONS: earn.flexible_subscription,
    earn.REDEMPTIONS: earn.flexible_redemption,
    earn.REWARDS: earn.flexible_reward,
    transfers.UNIVERSAL: transfers.universal,
    spot.DUST: spot.dust,
    transfers.CLOUD_MINING: transfers.cloud_mining,
    earn.LOCKED_SUBSCRIPTIONS: earn.locked_subscription,
    earn.LOCKED_REDEMPTIONS: earn.locked_earn_redemption,
    earn.LOCKED_REWARDS: earn.locked_reward,
    earn.ONCHAIN_SUBSCRIPTIONS: earn.onchain_subscription,
    earn.ONCHAIN_REDEMPTIONS: earn.onchain_redemption,
    earn.ONCHAIN_REWARDS: earn.onchain_reward,
    **staking.SWAP_HANDLERS,
    staking.BFUSD_REWARDS.label: staking.bfusd_reward,
    staking.RWUSD_REWARDS.label: staking.rwusd_reward,
    staking.SOFT_STAKING_REWARDS.label: staking.soft_staking_reward,
    staking.BOOST_REWARDS: staking.boost_reward,
    loans.BORROWS: loans.borrow,
    loans.REPAYS: loans.repay,
    loans.LIQUIDATIONS: loans.liquidation,
    loans.VIP_REPAYS: loans.vip_repay,
    margin.TRADES: margin.trade,
    margin.TRANSFERS: margin.transfer,
    margin.BORROW_REPAY: margin.borrow_repay,
    margin.INTEREST: margin.interest,
    margin.CAPITAL_FLOW: margin.capital_flow,
    futures.UM_INCOME: futures.income,
    futures.CM_INCOME: futures.income,
    options.BILL: options.bill,
    fiat.ORDERS: fiat.order,
    fiat.PAYMENTS: fiat.payment,
    fiat.C2C: fiat.c2c,
    fiat.PAY: fiat.pay,
    fiat.REBATES: fiat.rebate,
}
# The report labels whose rows post (each row of each read, keyed by the row's id): the
# products with no history endpoint.
POSITION_HANDLERS: dict[str, Handler] = {
    structured.DUAL_INVESTMENT: structured.dual_investment,
    structured.DISCOUNT_BUY: structured.discount_buy,
}
# Rule 3: every reward history whose rows a dividend can have paid.
REWARD_HISTORIES: tuple[earn.Rewards, ...] = (
    *earn.REWARD_HISTORIES,
    *staking.REWARD_HISTORIES,
    *fiat.REWARD_HISTORIES,
)
# Rule 2: the endpoints that list wallet-to-wallet movements, the one that posts a movement first
# (`transfers.link_movements`): the universal transfer history, then the margin transfer
# history, then the futures income TRANSFER rows, then the options bill TRANSFER rows.
MOVEMENT_LISTINGS: tuple[transfers.Listing, ...] = (
    transfers.UNIVERSAL_LISTING,
    margin.MARGIN_LISTING,
    futures.UM_LISTING,
    futures.CM_LISTING,
    options.LISTING,
)
# The history labels whose rows have typed tables, by raw record type.
_LABEL_OF = {
    record_type(label): label for label in layout.TABLE_LABELS if ENDPOINTS[label].kind == "history"
}
# The report labels whose reads are applied, by raw record type.
_REPORT_OF = {record_type(label): label for label in POSITION_HANDLERS}


def _body(label: str, row: Row) -> ApiValue:
    """One row as the label's answer would carry it, for the parser."""
    where = ENDPOINTS[label].rows
    if where == "":
        return dict(row)
    body: ApiValue = [dict(row)]
    if where != ".":
        for step in reversed(where.split(".")):
            body = {step: body}
    return body


async def _apply_report(run: Run, record: RawRecord) -> None:
    """Each position row of one read: its report tables are stored with the sync's reports
    (`balances.store_reports`); here only its postings."""
    label = _REPORT_OF[record.record_type]
    content = as_api(record.content)
    assert isinstance(content, dict)
    started = content["read_from_ms"]
    assert isinstance(started, int)
    name = structured.RECORDS[label]
    read: dict[str, object] = {
        "connection_id": run.connection.id,
        "snapshot_at": tables.moment(started),
    }
    root = layout.layout(label)[0]
    for row in parse(label, content["body"]).rows:
        ids = [tables.key_part(label, row, field) for field in ENDPOINTS[label].id_fields]
        # The row's own key in its report table: the read and the row's id fields.
        values = tables.columns_for(label, row)
        typed_key = {column: read.get(column, values.get(column)) for column in root.key}
        run.current = Current(label, record, ":".join([name, *ids]), typed_key)
        await POSITION_HANDLERS[label](run, row)
    run.current = None


async def _apply(run: Run, record: RawRecord) -> None:
    if record.record_type in _REPORT_OF:
        await _apply_report(run, record)
        return
    label = _LABEL_OF[record.record_type]
    content = as_api(record.content)
    occurrence: int | None = None
    if not ENDPOINTS[label].id_fields:
        assert isinstance(content, dict)
        held, number = content["row"], content["occurrence"]
        assert isinstance(held, dict) and isinstance(number, int)
        content, occurrence = held, number
    assert isinstance(content, dict)
    parsed = parse(label, _body(label, content))
    for flag in unknown_flags(label, parsed):
        run.flag(flag)
    [row] = parsed.rows

    name = record_type(label).removeprefix("binance.")
    root = layout.layout(label)[0]
    key: dict[str, object] = {"connection_id": run.connection.id}
    if occurrence is None:
        ids = [tables.key_part(label, row, field) for field in ENDPOINTS[label].id_fields]
    else:
        digest = tables.content_hash(row)
        key |= {layout.CONTENT_HASH: digest, layout.OCCURRENCE: occurrence}
        ids = [digest, str(occurrence)]
    await tables.store(run.command, label, key, row)
    values = tables.columns_for(label, row)
    typed_key = {name_: key[name_] if name_ in key else values[name_] for name_ in root.key}
    run.current = Current(label, record, ":".join([name, *ids]), typed_key)
    run.report.records += 1
    handler = HANDLERS.get(label)
    if handler is not None:
        await handler(run, row)
    run.current = None


async def _records(run: Run, after: UUID | None) -> list[RawRecord]:
    """The connection's history raw records, and the reads of its posting report labels,
    stored after `after` (all with None), in order."""
    r = core_models.RawRecord
    query = select(r.id).where(
        r.connection_id == run.connection.id,
        r.record_type.in_([*_LABEL_OF, *_REPORT_OF]),
    )
    if after is not None:
        query = query.where(r.id > after)
    ids = list(await run.session.scalars(query.order_by(r.id)))
    return await get_raw_records(run.session, ids)


def _rewards_sql(columns: str, condition: str) -> str:
    """Every reward history's rows as one set: (label, content_hash, occurrence, asset, amount,
    time, dividend_id), those of each history meeting `condition` (and its own `linkable` when
    `columns` asks for it)."""
    parts: list[str] = []
    for index, rewards in enumerate(REWARD_HISTORIES):
        where = condition.replace("{linkable}", f"({rewards.linkable})")
        parts.append(
            f"select :l{index} as label, content_hash, occurrence,"
            f" {rewards.asset_sql} as asset, {rewards.amount_sql} as amount,"
            f" {rewards.time_sql} as time, dividend_id"
            f" from finance.{rewards.table} where connection_id = :c and {where}"
        )
    return f"select {columns} from (" + " union all ".join(parts) + ") r"


def _labels() -> dict[str, str]:
    return {f"l{index}": rewards.label for index, rewards in enumerate(REWARD_HISTORIES)}


async def link_payouts(run: Run) -> None:
    """Rule 3: a reward and an asset dividend of the same asset and amount within 26 hours of
    each other are one payout, posted where it landed (the dividend, on Spot): the reward is
    linked to the dividend and its own posting removed. Every product's rewards can be
    (`REWARD_HISTORIES`), each only those of its rows known to be paid out (`linkable`): a
    flexible `REALTIME` reward accrues inside the Earn position (design 5.7), and a missing or
    other type is never linked; a distributed SOL boost reward is not paid out until claimed.

    The links are worked out afresh from every reward and dividend the connection has, so
    they do not depend on how the records were split across runs: candidate pairs are taken
    nearest first (ties by the reward's time, product and key, then the dividend's time and
    id), each reward and each dividend at most once. A reward whose link changes is updated;
    one no longer linked is posted again."""
    rewards_by_label = {rewards.label: rewards for rewards in REWARD_HISTORIES}
    candidates = _rewards_sql(
        "r.label, r.content_hash, r.occurrence, r.time, d.id as dividend_id, d.div_time",
        "{linkable}",
    )
    rows = await run.session.execute(
        text(
            f"{candidates}"
            " join finance.binance_dividends d on d.connection_id = :c"
            "  and d.asset = r.asset and d.amount = r.amount"
            "  and coalesce(d.direction, 1) = 1"
            "  and d.div_time between r.time - make_interval(hours => :h)"
            "   and r.time + make_interval(hours => :h)"
            " order by abs(extract(epoch from d.div_time - r.time)), r.time, r.label,"
            "  r.content_hash, r.occurrence, d.div_time, d.id"
        ),
        {"c": run.connection.id, "h": PAYOUT_WINDOW_HOURS, **_labels()},
    )
    wanted: dict[tuple[str, str, int], str] = {}
    paid: set[str] = set()
    for row in rows.all():
        reward = (row.label, row.content_hash, row.occurrence)
        if reward in wanted or row.dividend_id in paid:
            continue
        wanted[reward] = row.dividend_id
        paid.add(row.dividend_id)
    held = await run.session.execute(
        text(_rewards_sql("*", "dividend_id is not null")), {"c": run.connection.id, **_labels()}
    )
    current = {(r.label, r.content_hash, r.occurrence): r for r in held.all()}
    for reward in sorted(set(wanted) | set(current)):
        dividend_id = wanted.get(reward)
        before = current.get(reward)
        if before is not None and before.dividend_id == dividend_id:
            continue
        label, digest, occurrence = reward
        rewards = rewards_by_label[label]
        key = f"{rewards.record}:{digest}:{occurrence}"
        transaction_id = None
        if dividend_id is not None:
            await remove(run, key)
        else:
            assert before is not None
            transaction_id = await earn.post_reward(
                run, rewards, key, before.asset, before.amount, before.time
            )
        await run.session.execute(
            text(
                f"update finance.{rewards.table}"
                " set dividend_id = :d, transaction_id = :t"
                " where connection_id = :c and content_hash = :h and occurrence = :o"
            ),
            {
                "d": dividend_id,
                "t": transaction_id,
                "c": run.connection.id,
                "h": digest,
                "o": occurrence,
            },
        )


async def publish_flags(run: Run) -> list[str]:
    """Add this run's flags that `config["flags"]` does not hold yet to it and queue them for
    the sync's message (`notices`), in the same transaction, so a flag known is a flag told;
    keep every flag in the connection's warnings; return the new ones."""
    return await notices.publish(run.command, run.connection.id, run.report.flagged)


async def normalize(
    ctx: SyncContext,
    *,
    flags: Iterable[str] = (),
    reads: Mapping[str, Read | None] | None = None,
    daily: bool = False,
) -> NormalizeReport:
    """Apply the raw records stored since the last run; `flags` are the sync's own (the
    wallet checks), published with the run's. `reads` (the sync's settled reads) and, with
    `daily`, the first sync's daily snapshots are recorded as reports of the accounts that
    exist once the records are applied (`record_reads`). A new NORMALIZER_VERSION applies them
    all."""
    return await _normalize(ctx, everything=False, flags=flags, reads=reads, daily=daily)


async def regenerate(ctx: SyncContext) -> NormalizeReport:
    """Apply every raw record of the connection again."""
    return await _normalize(ctx, everything=True, flags=(), reads=None, daily=False)


async def _normalize(
    ctx: SyncContext,
    *,
    everything: bool,
    flags: Iterable[str],
    reads: Mapping[str, Read | None] | None,
    daily: bool,
) -> NormalizeReport:
    args: dict[str, JSONValue] = {"connection_id": str(ctx.connection.id)}
    with localcontext(prec=MAX_PREC):
        async with (
            ctx.session_factory() as session,
            command_context(session, ctx.command.actor, "binance.normalize", args) as cmd,
        ):
            connection = await get_connection(session, ctx.connection.id)
            config = connection.config
            through = config.get(NORMALIZED_THROUGH_KEY)
            replay = everything or config.get(VERSION_KEY) != NORMALIZER_VERSION
            after = UUID(through) if isinstance(through, str) and not replay else None
            run = await Run.start(ctx, cmd, connection)
            for flag in flags:
                run.flag(flag)
            last = after
            for record in await _records(run, after):
                await _apply(run, record)
                last = record.id
            await transfers.link_movements(run, MOVEMENT_LISTINGS)
            await link_payouts(run)
            await capital.link_travel_rule(run)
            if reads is not None:
                await record_reads(cmd, connection, reads, daily=daily, flag=run.flag)
            # After the reports: a flexible loan's interest is what they show the records do
            # not explain.
            await loans.set_interest(run)
            run.report.new_flags = await publish_flags(run)
            run.report.touched = run.touched
            await queue_touched(cmd, connection.id, run.touched)
            values: dict[str, JSONValue] = {}
            if last is not None and through != str(last):
                values[NORMALIZED_THROUGH_KEY] = str(last)
            if config.get(VERSION_KEY) != NORMALIZER_VERSION:
                values[VERSION_KEY] = NORMALIZER_VERSION
            if values:
                await update_config(cmd, connection.id, values)
            return run.report
