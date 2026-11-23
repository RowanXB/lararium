"""Trading 212 raw records into the ledger (design §5.5-5.11).

Deterministic from its inputs and versioned (NORMALIZER_VERSION, stored in the connection's
config, Decision 8): when the stored version differs, the run replays every raw record the
connection holds (the latest copy of each history and metadata item, and every report), so
what a new version stores is filled in for what an older one saw.

In order: the account (accounts.py); instrument and exchange metadata (reports.py); order
fills, dividends and cash movements as connector transactions, then the opening adjustments
(records.py); the summaries, positions, pending orders and pies, typed per day, with reported
snapshots, and prices (reports.py); reconciliation of this sync's report (reconcile.py).

Every record's fields are checked first: a required one missing fails the run; an unknown one
with a value is flagged. Flags (unknown fields, unknown values of the enums that decide
postings, types of instrument the ledger does not hold, a TRANSFER) are reported for the sync
to publish, as are the instruments still without a type, which every run finds in full.
"""

from collections import defaultdict
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import MAX_PREC, localcontext
from uuid import UUID

from lararium.core.connections import get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.trading212 import accounts, reconcile, records, reports
from lararium.modules.finance.trading212.fields import ENDPOINTS, MAP_ENTRIES
from lararium.modules.finance.trading212.instruments import unknown_types
from lararium.modules.finance.trading212.state import (
    NormalizeReport,
    Record,
    Run,
    Trading212DataError,
    record_key,
    records_of,
    stored,
    text_at,
)

__all__ = ["NORMALIZER_VERSION", "NormalizeReport", "Trading212DataError", "normalize"]

NORMALIZER_VERSION = "trading212-norm-1"
# The version that last normalized the connection, in its config (Decision 8).
NORMALIZER_VERSION_KEY = "normalizer_version"
# Items a key names once per connection, the ledger's (Decision 9) and the metadata's: the
# latest copy of each is what counts. The others are reports, each dated by its fetch.
_LEDGER = ("orders", "dividends", "transactions")
_KEYED = (*_LEDGER, "instruments", "exchanges")
_REPORTS = ("summary", "positions", "pending_orders", "pies", "pie")
# What each ledger key is, as an error names it.
_KEY_KIND = {"orders": "fill or order id", "dividends": "reference", "transactions": "reference"}
UNKNOWN_TYPE = "instrument type unknown"


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _leaves(value: JSONValue, path: str) -> Iterator[str]:
    """The paths of the item's values (fields.py's notation); null and empty ones have none."""
    if isinstance(value, dict):
        entries = MAP_ENTRIES.get(f"{path}{{}}")
        if entries is not None:
            key_field, value_field = entries
            for entry in value.values():
                yield f"{path}{{}}.{key_field}"
                yield from _leaves(entry, f"{path}{{}}.{value_field}")
            return
        for key, entry in value.items():
            yield from _leaves(entry, _join(path, key))
    elif isinstance(value, list):
        for entry in value:
            yield from _leaves(entry, f"{path}[]")
    elif value is not None:
        yield path


def _lacks(container: JSONValue, parts: Sequence[str], path: str = "") -> bool:
    """Whether a required path (split at its dots) is missing below `container`: one under an
    array or a map only where that array or map is there."""
    head, rest = parts[0], parts[1:]
    name = head.removesuffix("[]").removesuffix("{}")
    here = _join(path, head)
    value = container.get(name) if isinstance(container, dict) else None
    if head.endswith("[]"):
        if value is None:
            return False
        if not isinstance(value, list):
            return True
        return any(e is None if not rest else _lacks(e, rest, here) for e in value)
    if head.endswith("{}"):
        if value is None:
            return False
        key_field, value_field = MAP_ENTRIES[here]
        if not isinstance(value, dict) or not rest or rest[0] not in (key_field, value_field):
            return True
        if rest[0] == key_field:
            return False
        below = rest[1:]
        return any(v is None if not below else _lacks(v, below, here) for v in value.values())
    if not rest:
        return value is None
    if value is None:
        # A parent missing: the path is missing, unless it lies in an array or map.
        return not any(p.endswith(("[]", "{}")) for p in rest)
    return _lacks(value, rest, here)


def _check(run: Run, record: Record) -> None:
    """Required fields fail the run when missing; unknown ones with a value are flagged."""
    spec = ENDPOINTS[record.endpoint]
    for path in sorted(spec.required):
        if _lacks(record.content, path.split(".")):
            raise Trading212DataError(f"{spec.record_type} lacks {path}")
    for path in sorted(set(_leaves(record.content, "")) - spec.known):
        run.flag(f"{record.endpoint}.{path}")


async def _records(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], replay: bool
) -> tuple[list[Record], list[Record], list[Record]]:
    """(the keyed items, one per key; the reports; this run's own records). With `replay`,
    also the latest copy of every keyed item and every report the connection holds.

    One ledger key with two contents in one run fails it, naming the record type and the
    key's kind (Decision 9): an item repeated unchanged is one raw record."""
    session = ctx.command.session
    own = await records_of(session, raw_record_ids)
    keyed: dict[tuple[str, str], Record] = {}
    reported: dict[UUID, Record] = {}
    for record in sorted(own, key=lambda r: (r.fetched_at, r.id)):
        if record.endpoint in _REPORTS:
            reported[record.id] = record
            continue
        key = (record.endpoint, record_key(record))
        held = keyed.get(key)
        if held is not None and record.endpoint in _LEDGER and held.content != record.content:
            raise Trading212DataError(
                f"{record.record_type} has two different items with one"
                f" {_KEY_KIND[record.endpoint]}"
            )
        keyed[key] = record
    if replay:
        given = set(keyed)
        for record in await stored(session, ctx.connection.id, _KEYED):
            if (key := (record.endpoint, record_key(record))) not in given:
                keyed[key] = record  # oldest first, so the latest copy stays
        for record in await stored(session, ctx.connection.id, _REPORTS):
            reported.setdefault(record.id, record)
    return list(keyed.values()), list(reported.values()), own


def _tickers(by_endpoint: dict[str, list[Record]], reported: Sequence[Record]) -> set[str]:
    found = {text_at(r.content, "order.instrument.ticker") for r in by_endpoint["orders"]}
    found |= {text_at(r.content, "instrument.ticker") for r in by_endpoint["dividends"]}
    found |= {
        text_at(r.content, "instrument.ticker") for r in reported if r.endpoint == "positions"
    }
    return found - {""}


async def normalize(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], *, now: datetime | None = None
) -> NormalizeReport:
    """Apply raw records to the ledger; `now` is the sync's time (default: now).

    Its arithmetic on amounts is exact, as the ledger's balance check is: the default context
    rounds to 28 digits, which could leave postings that do not balance."""
    with localcontext(prec=MAX_PREC):
        return await _normalize(ctx, list(dict.fromkeys(raw_record_ids)), now or datetime.now(UTC))


async def _normalize(
    ctx: SyncContext, raw_record_ids: list[UUID], now: datetime
) -> NormalizeReport:
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    replay = connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
    keyed, reported, own = await _records(ctx, raw_record_ids, replay)
    report = NormalizeReport(replayed=replay)
    run = Run(ctx, connection, now, report)
    by_endpoint: defaultdict[str, list[Record]] = defaultdict(list)
    for record in sorted(keyed, key=lambda r: (record_key(r), r.fetched_at, r.id)):
        _check(run, record)
        by_endpoint[record.endpoint].append(record)
    for record in reported:
        _check(run, record)

    own_ids = {record.id for record in own}
    summaries = sorted(
        (r for r in own if r.endpoint == "summary"), key=lambda r: (r.fetched_at, r.id)
    )
    summary = summaries[-1] if summaries else None
    latest_stored = [r for r in reported if r.endpoint == "summary"]
    await accounts.account(
        run, summary or max(latest_stored, key=lambda r: (r.fetched_at, r.id), default=None)
    )

    await reports.instruments(run, by_endpoint["instruments"], replay)
    await reports.exchanges(run, by_endpoint["exchanges"], replay)
    await run.load_metadata(_tickers(by_endpoint, reported))
    for record in sorted(by_endpoint["orders"], key=records.order_time):
        await records.order(run, record)
    for record in sorted(
        by_endpoint["dividends"], key=lambda r: (text_at(r.content, "paidOn"), record_key(r))
    ):
        await records.dividend(run, record)
    given = {record_key(r) for r in by_endpoint["transactions"]}
    movements = by_endpoint["transactions"] + [
        r for r in await records.unidentified_transfers(run) if record_key(r) not in given
    ]
    for record in sorted(movements, key=lambda r: (text_at(r.content, "dateTime"), record_key(r))):
        await records.cash_movement(run, record)
    await records.openings(run)

    await reports.apply(run, reported, own_ids)
    await reports.prices(run)

    # Reconciliation (design §5.11), at the sync's time: only a run with this sync's summary
    # has what Trading 212 reports now; another keeps the warnings the last check left.
    if summary is not None:
        positions = [r for r in own if r.endpoint == "positions"]
        account = await get_account(run.session, run.account)
        mismatches = await reconcile.check(
            run.session,
            run.account,
            account.name,
            run.day,
            await reports.reported(run, summary, positions),
        )
        report.mismatches = [m.warning for m in mismatches]
        await reconcile.publish(ctx, mismatches)
    report.instrument_flags = {
        f"{UNKNOWN_TYPE} {ticker}" for ticker in await unknown_types(run.session, run.account)
    }

    # Only this normaliser's keys: others (the account kind) stay as they are.
    values: dict[str, JSONValue] = {
        accounts.ACCOUNT_MAP_KEY: run.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
    }
    if any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values)
    return report
