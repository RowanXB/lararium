"""Fetching a Bitcoin wallet from an Esplora server (design 5.2, 5.3).

One fetch: the chain tip, checked against what is stored; the recheck of transactions that had
fewer than 6 confirmations when last reported; the scan, each branch from index 0 until `gap`
addresses in a row have never had a transaction (an address list asks each address once), with
each changed address's history asked as soon as its stats show the change; then the wallet's
account and identifiers.

Everything fetched commits as it goes, in short transactions of its own through
`ctx.session_factory`, so a sync that fails keeps it. What survives is the history: the next
sync still asks every address's stats from index 0, but asks a history only where the reported
count (chain + mempool) differs from the one its last complete history held
(`bitcoin_addresses.history_tx_count`). An address's history commits whole, with that count,
so a history cut short is asked again in full. A transaction the recheck finds gone or back in
the mempool clears that count on the wallet's addresses it touches, so the scan that follows
asks their histories again and finds what replaced it (a fee bump, a double spend), though
every count is unchanged; so does one it finds in a block whose time the status does not give,
which the history's transaction then supplies.

What still needs normalizing is kept in the database too: every write that inserts a
transaction or changes its status or which of its inputs and outputs are the wallet's sets
`bitcoin_transactions.pending` in the same transaction, and `FetchResult` is read from the
pending rows. The normalizer calls `mark_normalized` in the sync's own transaction, so a sync
that fails anywhere after the fetch leaves them pending for the next one.

The caller must not hold the connection's row lock (`lock_connection`) in `ctx.command` when
it calls `fetch`: the fetch writes the connection's config, and `wallet_account` locks the row,
each in a session of its own, which would wait on that lock for ever.

No request, raw record request, audit entry or error names an address: a record's request is
the endpoint template and the address's place in the wallet.
"""

from collections.abc import AsyncGenerator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.bitcoin.accounts import wallet_account
from lararium.modules.finance.bitcoin.client import EsploraClient
from lararium.modules.finance.bitcoin.derive import Branch, Wallet
from lararium.modules.finance.bitcoin.parse import (
    AddressStats,
    Input,
    MalformedResponse,
    Status,
    Tx,
    unknown_fields,
)
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    identifier_account,
)
from lararium.modules.finance.readonly_http import JSONValue as ApiValue
from lararium.modules.finance.readonly_http import record_json
from lararium.modules.finance.report_sightings import record_sighting

VERSION = "bitcoin-sync-1"
# A transaction this shallow when last reported is asked again; deeper reorganisations are
# left to the balance check (design 6).
SAFE_CONFIRMATIONS = 6
# Confirmed transactions per history page (Esplora's `/txs` and `/txs/chain/:txid`).
CHAIN_PAGE = 25
# Bounds on what a misbehaving server can make a sync do: the addresses scanned per branch;
# how far the tip may move on from the last sync's (`allowed_jump`); and how far a block a
# response names may be above the tip (a load-balanced server's backends can differ by a
# block or two).
MAX_INDEX = 100_000
MAX_AHEAD = 2
# The last accepted tip and the wall time it was read (ISO, UTC), in the connection's config.
TIP_KEY = "tip_height"
TIP_AT_KEY = "tip_at"

TIP = "/blocks/tip/height"
STATS = "/address/:address"
TXS = "/address/:address/txs"
TXS_AFTER = "/address/:address/txs/chain/:last_seen_txid"
STATUS = "/tx/:txid/status"


class TooManyAddresses(DisplayableError):
    """The gap rule did not end within `MAX_INDEX` addresses of a branch."""

    def __init__(self) -> None:
        super().__init__("The wallet has more addresses than Lararium scans.")


@dataclass
class FetchResult:
    tip: int
    # Every address scanned this sync, with its stats. Addresses are the owner's: never
    # logged, so they stay out of the repr.
    scanned: dict[str, AddressStats] = field(repr=False)
    # The pending transactions (module docstring), less those in `left_chain`: new or changed
    # since they were last normalized, by this fetch or one whose sync failed later.
    changed_txids: set[str]
    # The pending transactions that were confirmed when last normalized and are not now.
    left_chain: set[str]
    # Scanned addresses registered to another account, which keeps them (design 5.3).
    conflicts: list[str] = field(repr=False)
    # Field paths of the responses that the documented format does not list.
    unknown: set[str]


@dataclass(frozen=True)
class _Position:
    """An address's place in the wallet: what a raw record's request names instead of it."""

    source: str
    branch: Branch
    index: int

    def request(self, endpoint: str, **extra: JSONValue) -> dict[str, JSONValue]:
        return {
            "endpoint": endpoint,
            "source": self.source,
            "branch": self.branch,
            "index": self.index,
            **extra,
        }

    def args(self, connection_id: UUID) -> dict[str, JSONValue]:
        """An audit entry's arguments."""
        return {
            "connection_id": str(connection_id),
            "source": self.source,
            "branch": self.branch,
            "index": self.index,
        }


@dataclass(frozen=True)
class _Page:
    request: dict[str, JSONValue]
    body: ApiValue
    txs: list[Tx]


def _now() -> datetime:
    return datetime.now(UTC)


def _status_row(status: Status) -> tuple[bool, int | None, str | None, datetime | None]:
    return (status.confirmed, status.block_height, status.block_hash, status.block_time)


_UPSERT_ADDRESS = text(
    """
    insert into finance.bitcoin_addresses
      (connection_id, source, branch, address_index, address, script_type,
       first_used_at, last_used_at)
    values (:c, :source, :branch, :index, :address, :script_type, :used_at, :used_at)
    on conflict (connection_id, source, branch, address_index) do update set
      address = excluded.address,
      script_type = excluded.script_type,
      first_used_at = coalesce(finance.bitcoin_addresses.first_used_at, excluded.first_used_at),
      last_used_at = coalesce(excluded.last_used_at, finance.bitcoin_addresses.last_used_at)
    """
)
_INSERT_REPORT = text(
    """
    insert into finance.bitcoin_address_reports
      (connection_id, sync_id, address,
       chain_tx_count, chain_funded_txo_count, chain_funded_txo_sum,
       chain_spent_txo_count, chain_spent_txo_sum,
       mempool_tx_count, mempool_funded_txo_count, mempool_funded_txo_sum,
       mempool_spent_txo_count, mempool_spent_txo_sum)
    values (:c, :sync_id, :address, :c_tx, :c_fc, :c_fs, :c_sc, :c_ss,
            :m_tx, :m_fc, :m_fs, :m_sc, :m_ss)
    on conflict do nothing
    """
)
_TX_FIELDS = (
    "version",
    "locktime",
    "size",
    "weight",
    "sigops",
    "fee",
    "confirmed",
    "block_height",
    "block_hash",
    "block_time",
)
_UPSERT_TX = text(
    f"""
    insert into finance.bitcoin_transactions
      (connection_id, txid, {", ".join(_TX_FIELDS)}, first_seen_at, last_seen_at, seen_at_height,
       pending)
    values (:c, :txid, {", ".join(f":{name}" for name in _TX_FIELDS)}, :seen, :seen, :tip,
            :pending)
    on conflict (connection_id, txid) do update set
      {", ".join(f"{name} = excluded.{name}" for name in _TX_FIELDS)},
      last_seen_at = excluded.last_seen_at,
      seen_at_height = excluded.seen_at_height,
      pending = finance.bitcoin_transactions.pending or excluded.pending
    """
)
# An input or output is the wallet's when its address is one the wallet has scanned; a row
# stored before its address was scanned is corrected at the end of the fetch (`_own`).
_OWN = (
    "exists (select 1 from finance.bitcoin_addresses a"
    " where a.connection_id = :c and a.address = :{})"
)
_INPUT_FIELDS = (
    "prev_txid",
    "prev_vout",
    "is_coinbase",
    "scriptsig",
    "scriptsig_asm",
    "inner_redeemscript_asm",
    "inner_witnessscript_asm",
    "sequence",
    "witness",
    "prevout_scriptpubkey",
    "prevout_scriptpubkey_asm",
    "prevout_scriptpubkey_type",
    "prevout_scriptpubkey_address",
    "prevout_value",
)
_UPSERT_INPUT = text(
    f"""
    insert into finance.bitcoin_inputs
      (connection_id, txid, input_index, {", ".join(_INPUT_FIELDS)}, own)
    values (:c, :txid, :input_index, {", ".join(f":{name}" for name in _INPUT_FIELDS)},
            {_OWN.format("prevout_scriptpubkey_address")})
    on conflict (connection_id, txid, input_index) do update set
      {", ".join(f"{name} = excluded.{name}" for name in _INPUT_FIELDS)}
    """
).bindparams(bindparam("witness", type_=ARRAY(Text)))
_OUTPUT_FIELDS = (
    "scriptpubkey",
    "scriptpubkey_asm",
    "scriptpubkey_type",
    "scriptpubkey_address",
    "value",
)
_UPSERT_OUTPUT = text(
    f"""
    insert into finance.bitcoin_outputs
      (connection_id, txid, vout, {", ".join(_OUTPUT_FIELDS)}, own)
    values (:c, :txid, :vout, {", ".join(f":{name}" for name in _OUTPUT_FIELDS)},
            {_OWN.format("scriptpubkey_address")})
    on conflict (connection_id, txid, vout) do update set
      {", ".join(f"{name} = excluded.{name}" for name in _OUTPUT_FIELDS)}
    """
)
_FIX_OWN = [
    text(
        f"""
        update finance.bitcoin_{table} r set own = not r.own
        where r.connection_id = :c and r.own <> exists (
          select 1 from finance.bitcoin_addresses a
          where a.connection_id = r.connection_id and a.address = r.{column})
        returning r.txid
        """
    )
    for table, column in (
        ("inputs", "prevout_scriptpubkey_address"),
        ("outputs", "scriptpubkey_address"),
    )
]
_MARK_PENDING = text(
    "update finance.bitcoin_transactions set pending = true where connection_id = :c and txid in :t"
).bindparams(bindparam("t", expanding=True))
# The wallet's addresses a transaction touches forget their last history's count, so the scan
# asks those histories again.
_FORGET_HISTORIES = text(
    """
    update finance.bitcoin_addresses set history_tx_count = null
    where connection_id = :c and address in (
      select prevout_scriptpubkey_address from finance.bitcoin_inputs
      where connection_id = :c and txid = :t
      union
      select scriptpubkey_address from finance.bitcoin_outputs
      where connection_id = :c and txid = :t)
    """
)


def allowed_jump(elapsed: timedelta) -> int:
    """How many blocks the tip may grow in `elapsed`: blocks average 10 minutes but bunch up,
    so twice that rate, plus 100."""
    minutes = max(int(elapsed.total_seconds() // 60), 0)
    return 100 + minutes // 5


def _previous_tip(config: Mapping[str, JSONValue]) -> tuple[int, datetime] | None:
    height, at = config.get(TIP_KEY), config.get(TIP_AT_KEY)
    if not isinstance(height, int) or isinstance(height, bool) or not isinstance(at, str):
        return None  # the first sync (or a config without both): nothing to compare with
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None
    return height, when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _depth(tip: int, height: int) -> int:
    """Confirmations of a block at `height` when the tip is `tip`; never negative."""
    return max(tip - height + 1, 0)


async def mark_normalized(
    session: AsyncSession, connection_id: UUID, txids: Collection[str]
) -> None:
    """Clear `pending` on the transactions the normalizer has posted (or voided), recording
    whether each was confirmed then. Call it in the sync's own transaction: a rollback leaves
    them pending for the next sync."""
    if not txids:
        return
    await session.execute(
        text(
            "update finance.bitcoin_transactions"
            " set pending = false, normalized_confirmed = (confirmed is true)"
            " where connection_id = :c and txid in :t"
        ).bindparams(bindparam("t", expanding=True)),
        {"c": connection_id, "t": sorted(set(txids))},
    )


def _input_row(index: int, vin: Input) -> dict[str, object]:
    prevout = vin.prevout  # None only for a coinbase input
    return {
        "input_index": index,
        "prev_txid": vin.txid,
        "prev_vout": vin.vout,
        "is_coinbase": vin.is_coinbase,
        "scriptsig": vin.scriptsig,
        "scriptsig_asm": vin.scriptsig_asm,
        "inner_redeemscript_asm": vin.inner_redeemscript_asm,
        "inner_witnessscript_asm": vin.inner_witnessscript_asm,
        "sequence": vin.sequence,
        "witness": list(vin.witness),
        "prevout_scriptpubkey": None if prevout is None else prevout.scriptpubkey,
        "prevout_scriptpubkey_asm": None if prevout is None else prevout.scriptpubkey_asm,
        "prevout_scriptpubkey_type": None if prevout is None else prevout.scriptpubkey_type,
        "prevout_scriptpubkey_address": None if prevout is None else prevout.scriptpubkey_address,
        "prevout_value": None if prevout is None else prevout.value,
    }


class _Fetch:
    def __init__(self, ctx: SyncContext, client: EsploraClient, wallet: Wallet) -> None:
        self.ctx = ctx
        self.client = client
        self.wallet = wallet
        self.tip = 0
        # From before the scan: each address's last complete history (count, tip), and the
        # transactions stored as confirmed.
        self.histories: dict[str, tuple[int | None, int | None]] = {}
        self.stored_confirmed: set[str] = set()
        self.scanned: dict[str, AddressStats] = {}
        self.unknown: set[str] = set()

    @asynccontextmanager
    async def _command(
        self, name: str, args: Mapping[str, JSONValue] | None = None
    ) -> AsyncGenerator[CommandContext]:
        args = dict(args or {"connection_id": str(self.ctx.connection.id)})
        async with (
            self.ctx.session_factory() as session,
            command_context(session, self.ctx.command.actor, f"bitcoin.{name}", args) as cmd,
        ):
            yield cmd

    async def _raw(
        self,
        cmd: CommandContext,
        record_type: str,
        request: dict[str, JSONValue],
        content: JSONValue,
    ) -> UUID:
        return await store_raw_record(
            cmd,
            connection_id=self.ctx.connection.id,
            record_type=record_type,
            request=request,
            content=content,
            connector_version=VERSION,
            sync_id=self.ctx.sync_id,
        )

    async def run(self) -> FetchResult:
        await self._tip()
        await self._recheck()
        await self._load()
        for source in self.wallet.sources:
            if not source.gaps:
                # An address list: no branches to derive, each address asked once.
                for index in range(len(self.wallet.listed())):
                    await self._address(_Position(source.name, 0, index))
                continue
            for branch, gap in sorted(source.gaps.items()):
                index, unused = 0, 0
                while unused < gap:
                    if index >= MAX_INDEX:
                        raise TooManyAddresses
                    used = await self._address(_Position(source.name, branch, index))
                    unused = 0 if used else unused + 1
                    index += 1
        await self._own()
        account_id = await wallet_account(self.ctx, await self._opened_on())
        conflicts = await self._register(account_id)
        changed, left = await self._pending()
        return FetchResult(
            tip=self.tip,
            scanned=self.scanned,
            changed_txids=changed,
            left_chain=left,
            conflicts=conflicts,
            unknown=self.unknown,
        )

    async def _tip(self) -> None:
        """The chain tip, refused when it is below a block a deep stored transaction is in,
        or further on from the last sync's than the chain could have grown since.

        Only rows 6 blocks deep when last reported floor it: a shallower one may be a block a
        server got wrong, which the recheck that follows corrects."""
        tip = await self.client.tip_height()
        read_at = _now()
        connection_id = self.ctx.connection.id
        async with self.ctx.session_factory() as session:
            highest: int | None = await session.scalar(
                text(
                    "select max(block_height) from finance.bitcoin_transactions"
                    " where connection_id = :c and confirmed is true"
                    " and seen_at_height is not null"
                    " and greatest(seen_at_height - block_height + 1, 0) >= :n"
                ),
                {"c": connection_id, "n": SAFE_CONFIRMATIONS},
            )
            previous = _previous_tip((await get_connection(session, connection_id)).config)
        if highest is not None and tip < highest:
            raise MalformedResponse("tip height", "the body")
        if previous is not None and tip > previous[0] + allowed_jump(read_at - previous[1]):
            raise MalformedResponse("tip height", "the body")
        self.tip = tip
        async with self._command("tip") as cmd:
            # The body is the bare number, so this is the record of it exactly.
            await self._raw(cmd, "tip", {"endpoint": TIP}, record_json(Decimal(tip)))
            await update_config(cmd, connection_id, {TIP_KEY: tip, TIP_AT_KEY: read_at.isoformat()})

    def _check_height(self, status: Status, endpoint: str, field: str) -> None:
        """A block above the tip (beyond what servers behind one name differ by) is refused:
        stored, it would floor every later tip."""
        if status.block_height is not None and status.block_height > self.tip + MAX_AHEAD:
            raise MalformedResponse(endpoint, field)

    async def _load(self) -> None:
        params = {"c": self.ctx.connection.id}
        async with self.ctx.session_factory() as session:
            rows = await session.execute(
                text(
                    "select address, history_tx_count, history_height"
                    " from finance.bitcoin_addresses where connection_id = :c"
                ),
                params,
            )
            self.histories = {r.address: (r.history_tx_count, r.history_height) for r in rows}
            found = await session.execute(
                text(
                    "select txid from finance.bitcoin_transactions"
                    " where connection_id = :c and confirmed is true"
                ),
                params,
            )
            self.stored_confirmed = set(found.scalars())

    async def _address(self, position: _Position) -> bool:
        """Scan one address, and fetch its history if it changed; whether it was ever used."""
        address, script_type = self.wallet.address(position.source, position.branch, position.index)
        stats, body = await self.client.stats(address)
        seen_at = _now()
        if stats.address != address:
            raise MalformedResponse("address", "address")
        self.unknown |= unknown_fields(body)
        self.scanned[address] = stats
        total = stats.chain.tx_count + stats.mempool.tx_count
        async with self._command("address", position.args(self.ctx.connection.id)) as cmd:
            params = {"c": self.ctx.connection.id}
            await cmd.session.execute(
                _UPSERT_ADDRESS,
                params
                | {
                    "source": position.source,
                    "branch": position.branch,
                    "index": position.index,
                    "address": address,
                    "script_type": script_type,
                    "used_at": seen_at if total else None,
                },
            )
            raw_id = await self._raw(cmd, "address", position.request(STATS), record_json(body))
            # Address stats are reports: each sync's sighting dates them.
            await record_sighting(
                cmd,
                connection_id=self.ctx.connection.id,
                raw_record_id=raw_id,
                sync_id=self.ctx.sync_id,
                seen_at=seen_at,
            )
            chain, mempool = stats.chain, stats.mempool
            await cmd.session.execute(
                _INSERT_REPORT,
                params
                | {
                    "sync_id": self.ctx.sync_id,
                    "address": address,
                    "c_tx": chain.tx_count,
                    "c_fc": chain.funded_txo_count,
                    "c_fs": chain.funded_txo_sum,
                    "c_sc": chain.spent_txo_count,
                    "c_ss": chain.spent_txo_sum,
                    "m_tx": mempool.tx_count,
                    "m_fc": mempool.funded_txo_count,
                    "m_fs": mempool.funded_txo_sum,
                    "m_sc": mempool.spent_txo_count,
                    "m_ss": mempool.spent_txo_sum,
                },
            )
        stored_count, stored_height = self.histories.get(address, (None, None))
        if total != (stored_count or 0):
            await self._history(position, address, stats, stored_height)
        return total > 0

    def _reaches_stored(self, txs: Sequence[Tx], stored_height: int | None) -> bool:
        """Whether the page holds a transaction the address's last complete history stored
        already 6 blocks deep: everything older was stored with it. (A transaction stored by
        another address's history says nothing about this one's.)"""
        if stored_height is None:
            return False
        return any(
            tx.status.block_height is not None
            and tx.txid in self.stored_confirmed
            and _depth(stored_height, tx.status.block_height) >= SAFE_CONFIRMATIONS
            for tx in txs
        )

    async def _history(
        self, position: _Position, address: str, stats: AddressStats, stored_height: int | None
    ) -> None:
        """The address's history, newest first, until it reaches what was stored or ends;
        stored whole, with the count it holds, in one transaction.

        More transactions than the stats count is a server misbehaving (a walk that would
        never end) and fails the sync. The count is recorded only when the history is known
        whole: the walk's own count when it ran to the end, the stats' when it reached what
        was stored, none when the first page left out mempool transactions (Esplora lists 50
        at most), so the next sync asks again."""
        total = stats.chain.tx_count + stats.mempool.tx_count
        txs, body = await self.client.txs(address)
        for tx in txs:
            self._check_height(tx.status, "txs", "status.block_height")
        pages = [_Page(position.request(TXS), body, txs)]
        seen = {tx.txid for tx in txs}
        if len(seen) > total:
            raise MalformedResponse("txs", "the body")
        truncated = sum(1 for tx in txs if not tx.status.confirmed) < stats.mempool.tx_count
        ended = False
        while not self._reaches_stored(txs, stored_height):
            confirmed = [tx for tx in txs if tx.status.confirmed]
            if len(confirmed) < CHAIN_PAGE:
                ended = True
                break
            after = confirmed[-1].txid
            txs, body = await self.client.txs(address, after)
            for tx in txs:
                self._check_height(tx.status, "txs", "status.block_height")
            if any(tx.txid in seen or not tx.status.confirmed for tx in txs):
                # A page repeating or out of order would walk forever or miss transactions.
                raise MalformedResponse("txs", "the body")
            seen |= {tx.txid for tx in txs}
            if len(seen) > total:
                raise MalformedResponse("txs", "the body")
            pages.append(_Page(position.request(TXS_AFTER, last_seen_txid=after), body, txs))
        count = None if truncated else len(seen) if ended else total
        seen_at = _now()
        async with self._command("history", position.args(self.ctx.connection.id)) as cmd:
            for page in pages:
                self.unknown |= unknown_fields(page.body)
                await self._raw(cmd, "txs", page.request, record_json(page.body))
            await self._store_txs(cmd.session, [tx for page in pages for tx in page.txs], seen_at)
            await cmd.session.execute(
                text(
                    "update finance.bitcoin_addresses"
                    " set history_tx_count = :n, history_height = :tip"
                    " where connection_id = :c and address = :a"
                ),
                {"n": count, "tip": self.tip, "c": self.ctx.connection.id, "a": address},
            )

    async def _store_txs(self, session: AsyncSession, txs: Sequence[Tx], seen_at: datetime) -> None:
        if not txs:
            return
        c = self.ctx.connection.id
        stored = {
            row[0]: tuple(row[1:])
            for row in await session.execute(
                text(
                    f"select txid, {', '.join(_TX_FIELDS)} from finance.bitcoin_transactions"
                    " where connection_id = :c and txid in :t"
                ).bindparams(bindparam("t", expanding=True)),
                {"c": c, "t": sorted({tx.txid for tx in txs})},
            )
        }
        unique = {tx.txid: tx for tx in txs}
        for tx in unique.values():
            values = (tx.version, tx.locktime, tx.size, tx.weight, tx.sigops, tx.fee)
            values += _status_row(tx.status)
            await session.execute(
                _UPSERT_TX,
                {
                    "c": c,
                    "txid": tx.txid,
                    "seen": seen_at,
                    "tip": self.tip,
                    "pending": stored.get(tx.txid) != values,
                }
                | dict(zip(_TX_FIELDS, values, strict=True)),
            )
            if tx.vin:
                await session.execute(
                    _UPSERT_INPUT,
                    [
                        {"c": c, "txid": tx.txid} | _input_row(n, vin)
                        for n, vin in enumerate(tx.vin)
                    ],
                )
            if tx.vout:
                await session.execute(
                    _UPSERT_OUTPUT,
                    [
                        {
                            "c": c,
                            "txid": tx.txid,
                            "vout": n,
                            "scriptpubkey": out.scriptpubkey,
                            "scriptpubkey_asm": out.scriptpubkey_asm,
                            "scriptpubkey_type": out.scriptpubkey_type,
                            "scriptpubkey_address": out.scriptpubkey_address,
                            "value": out.value,
                        }
                        for n, out in enumerate(tx.vout)
                    ],
                )

    async def _recheck(self) -> None:
        """Ask again for every stored transaction that had fewer than 6 confirmations when
        the server last reported it. One gone, back in the mempool, or confirmed in a block
        whose time is not known makes the scan ask the histories of the wallet's addresses it
        touches (module docstring)."""
        async with self.ctx.session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "select txid, confirmed, block_height, block_hash, block_time"
                        " from finance.bitcoin_transactions"
                        " where connection_id = :c and seen_at_height is not null"
                        " and (confirmed is not true"
                        " or greatest(seen_at_height - block_height + 1, 0) < :n)"
                        " order by txid"
                    ),
                    {"c": self.ctx.connection.id, "n": SAFE_CONFIRMATIONS},
                )
            ).all()
        for row in rows:
            status, body = await self.client.status(row.txid)
            if status is not None:
                self._check_height(status, "status", "block_height")
            seen_at = _now()
            if body is not None:
                self.unknown |= unknown_fields(body)
            args: dict[str, JSONValue] = {
                "connection_id": str(self.ctx.connection.id),
                "txid": row.txid,
            }
            ids = {"c": self.ctx.connection.id, "t": row.txid}
            async with self._command("status", args) as cmd:
                # A status does not name its transaction, and two in one block read the
                # same: the record carries the one asked for, and null for a 404.
                await self._raw(
                    cmd,
                    "tx_status",
                    {"endpoint": STATUS, "txid": row.txid},
                    {"txid": row.txid, "status": record_json(body)},
                )
                before = (row.confirmed, row.block_height, row.block_hash, row.block_time)
                if status is None:
                    after = (False, None, None, None)
                    sql = (
                        "update finance.bitcoin_transactions set confirmed = false,"
                        " block_height = null, block_hash = null, block_time = null,"
                        " seen_at_height = null, pending = pending or :changed"
                        " where connection_id = :c and txid = :t"
                    )
                    params: dict[str, object] = {}
                else:
                    when = status.block_time
                    if when is None and status.block_hash == row.block_hash:
                        when = row.block_time  # the same block: its time is known
                    after = (status.confirmed, status.block_height, status.block_hash, when)
                    sql = (
                        "update finance.bitcoin_transactions set confirmed = :confirmed,"
                        " block_height = :height, block_hash = :hash, block_time = :time,"
                        " last_seen_at = :seen, seen_at_height = :tip,"
                        " pending = pending or :changed"
                        " where connection_id = :c and txid = :t"
                    )
                    params = {
                        "confirmed": after[0],
                        "height": after[1],
                        "hash": after[2],
                        "time": after[3],
                        "seen": seen_at,
                        "tip": self.tip,
                    }
                params["changed"] = after != before
                await cmd.session.execute(text(sql), ids | params)
                gone = status is None or (row.confirmed is True and not status.confirmed)
                # Confirmed in a block whose time the status does not give (Esplora documents
                # none): the history's full transaction carries it, and a mempool-to-block move
                # leaves the address's count as it was, so its history is asked again too.
                undated = status is not None and status.confirmed and after[3] is None
                if gone or undated:
                    await cmd.session.execute(_FORGET_HISTORIES, ids)

    async def _own(self) -> None:
        """Mark the inputs and outputs whose address the wallet now has, or no longer has, as
        stored before that address was scanned (a raised gap limit finds later addresses);
        their transactions become pending."""
        async with self._command("own") as cmd:
            flipped: set[str] = set()
            for statement in _FIX_OWN:
                found = await cmd.session.execute(statement, {"c": self.ctx.connection.id})
                flipped |= set(found.scalars())
            if flipped:
                await cmd.session.execute(
                    _MARK_PENDING, {"c": self.ctx.connection.id, "t": sorted(flipped)}
                )

    async def _opened_on(self) -> date | None:
        async with self.ctx.session_factory() as session:
            first: datetime | None = await session.scalar(
                text(
                    "select min(block_time) from finance.bitcoin_transactions"
                    " where connection_id = :c and confirmed is true"
                ),
                {"c": self.ctx.connection.id},
            )
        return None if first is None else first.astimezone(UTC).date()

    async def _register(self, account_id: UUID) -> list[str]:
        """Make every scanned address a `chain_address` of the account; those registered to
        another account stay there and are returned."""
        conflicts: list[str] = []
        async with self._command("identifiers") as cmd:
            for address in self.scanned:
                identifier_id = await get_or_create_identifier(cmd, "chain_address", address)
                await ensure_identifier(cmd, account_id, "chain_address", address)
                if await identifier_account(cmd.session, identifier_id) != account_id:
                    conflicts.append(address)
        return conflicts

    async def _pending(self) -> tuple[set[str], set[str]]:
        """(changed, left the chain) among the pending transactions."""
        async with self.ctx.session_factory() as session:
            rows = await session.execute(
                text(
                    "select txid, confirmed is true as confirmed, normalized_confirmed"
                    " from finance.bitcoin_transactions where connection_id = :c and pending"
                ),
                {"c": self.ctx.connection.id},
            )
            changed: set[str] = set()
            left: set[str] = set()
            for row in rows:
                (left if row.normalized_confirmed and not row.confirmed else changed).add(row.txid)
        return changed, left


async def fetch(ctx: SyncContext, client: EsploraClient, wallet: Wallet) -> FetchResult:
    """Scan the wallet, store what changed, and register its addresses (module docstring:
    call it without the connection's row lock held, and call `mark_normalized` on what the
    normalizer posts)."""
    return await _Fetch(ctx, client, wallet).run()
