"""A Bitcoin wallet's chain transactions into the ledger (design 4.4, 5.4, 5.5).

Read from the typed rows the fetch stored, never from raw records. Each chain transaction
becomes one ledger transaction per movement on the wallet's account, dated by its block
(`block_time`; nothing assumes block times increase) and carrying its chain, hash and the
address the movement is at. Which movements, by the transaction's inputs that are the
wallet's (`own`):

- none (a receipt, a coinbase's included): one per output paying the wallet,
  `<txid>:<vout>`, against `transfer_contra` and naming no counterparty, since the sender's
  addresses say nothing about whose account it came from;
- all (a spend): one per output paying another address, `<txid>:<vout>`, its counterparty
  that address's `chain_address` identifier (none for an output with no address), and the
  fee, `<txid>:fee` (the inputs less the outputs, `is_fee` against Unassigned); outputs
  paying the wallet are change and post nothing;
- some (a shared transaction: a coinjoin, a payjoin): one, `<txid>:net`, the wallet's outputs
  less its inputs against Unassigned, never a transfer leg, flagged.

So the movements of a chain transaction always add up to its outputs to the wallet less its
inputs from it. A zero-value output (OP_RETURN) posts nothing.

Only a confirmed transaction posts. One that is not confirmed now has its movements voided
(by the connector); one that was confirmed when last normalized has left the chain: its
`left_chain_at` is set and flagged. When it confirms again it posts again under the same
keys, which the ledger's upsert un-voids, with the new block's time. A movement that no
longer applies (an output found to be the wallet's own) is voided.

Every transaction normalized is marked (`fetch.mark_normalized`) in the sync's own
transaction, except a confirmed one whose block time is not known, which cannot be dated and
stays pending. When the connection's `normalizer_version` is not this one, every stored
transaction is normalized again (the replay), and the version is recorded.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar, Literal
from uuid import UUID

from sqlalchemy import select, text

from lararium.core.connections import get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.modules.finance import models
from lararium.modules.finance.bitcoin.accounts import ACCOUNT_KEY
from lararium.modules.finance.bitcoin.fetch import mark_normalized
from lararium.modules.finance.bitcoin.instruments import bitcoin
from lararium.modules.finance.connector_run import ConnectorRun
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.ledger import ChainFields, CounterpartyFields, PostingInput

NORMALIZER_VERSION = "bitcoin-norm-1"
# The version that last normalized the connection, in its config.
NORMALIZER_VERSION_KEY = "normalizer_version"
CHAIN = "bitcoin"
# Flags name no hash and no address.
SHARED_FLAG = "shared transaction"
LEFT_CHAIN_FLAG = "a confirmed transaction left the chain"
# What Suggestions shows for an address paid.
COUNTERPARTY_NAME = "Bitcoin address"
_DESCRIPTIONS = {
    "receipt": "Received bitcoin",
    "payment": "Sent bitcoin",
    "fee": "Bitcoin network fee",
    "net": "Shared transaction",
}


class BitcoinDataError(DisplayableError):
    """Stored chain data that cannot be posted; names the transaction, never an address."""

    def __init__(self, txid: str, problem: str) -> None:
        super().__init__(f"Bitcoin transaction {txid}: {problem}")


@dataclass
class NormalizeReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    flagged: set[str] = field(default_factory=set[str])


class _Run(ConnectorRun):
    # The normalizer writes no typed rows through the run (`typed` is unused).
    COLUMNS: ClassVar = {}
    RECORD_TYPES: ClassVar = {}
    CODE: ClassVar = re.compile(r"[a-z_]{1,32}")


@dataclass(frozen=True)
class Input:
    own: bool
    value: int  # satoshis; 0 for a coinbase input, which spends nothing


@dataclass(frozen=True)
class Output:
    vout: int
    own: bool
    value: int
    address: str | None


@dataclass(frozen=True)
class Movement:
    key: str
    kind: Literal["receipt", "payment", "fee", "net"]
    sats: int  # the wallet's change, signed
    vout: int | None = None
    address: str | None = None


def movements(txid: str, inputs: Collection[Input], outputs: Collection[Output]) -> list[Movement]:
    """The ledger movements of a confirmed chain transaction (module docstring)."""
    own_in = [i for i in inputs if i.own]
    if not own_in:
        return [
            Movement(f"{txid}:{o.vout}", "receipt", o.value, o.vout, o.address)
            for o in outputs
            if o.own and o.value > 0
        ]
    if len(own_in) == len(inputs):
        found = [
            Movement(f"{txid}:{o.vout}", "payment", -o.value, o.vout, o.address)
            for o in outputs
            if not o.own and o.value > 0
        ]
        fee = sum(i.value for i in inputs) - sum(o.value for o in outputs)
        if fee < 0:
            raise BitcoinDataError(txid, "its outputs exceed its inputs")
        if fee > 0:
            found.append(Movement(f"{txid}:fee", "fee", -fee))
        return found
    net = sum(o.value for o in outputs if o.own) - sum(i.value for i in own_in)
    return [Movement(f"{txid}:net", "net", net)] if net else []


def _btc(sats: int) -> Decimal:
    return Decimal(sats).scaleb(-8)


@dataclass(frozen=True)
class _Stored:
    txid: str
    confirmed: bool
    block_time: datetime | None
    normalized_confirmed: bool
    inputs: list[Input]
    outputs: list[Output]


# Every query below reads the rows of `:t`, or of every stored transaction when it is null.
_ONLY = " and (cast(:t as text[]) is null or txid = any(cast(:t as text[])))"
_TRANSACTIONS = text(
    "select txid, confirmed is true as confirmed, block_time, normalized_confirmed,"
    " block_height from finance.bitcoin_transactions where connection_id = :c" + _ONLY
)
_INPUTS = text(
    "select txid, own, coalesce(prevout_value, 0) as value from finance.bitcoin_inputs"
    " where connection_id = :c" + _ONLY
)
_OUTPUTS = text(
    "select txid, vout, own, value, scriptpubkey_address from finance.bitcoin_outputs"
    " where connection_id = :c" + _ONLY + " order by vout"
)


async def _stored(run: _Run, txids: Collection[str] | None) -> list[_Stored]:
    """The stored transactions (every one for None), by block, then id."""
    params = {"c": run.connection_id, "t": None if txids is None else sorted(set(txids))}
    heights: dict[str, int | None] = {}
    found: dict[str, _Stored] = {}
    for r in await run.session.execute(_TRANSACTIONS, params):
        heights[r.txid] = r.block_height
        found[r.txid] = _Stored(r.txid, r.confirmed, r.block_time, r.normalized_confirmed, [], [])
    for r in await run.session.execute(_INPUTS, params):
        found[r.txid].inputs.append(Input(r.own, r.value))
    for r in await run.session.execute(_OUTPUTS, params):
        found[r.txid].outputs.append(Output(r.vout, r.own, r.value, r.scriptpubkey_address))
    return sorted(
        found.values(),
        key=lambda s: (heights[s.txid] is None, heights[s.txid] or 0, s.txid),
    )


class _Normalizer:
    def __init__(self, run: _Run, account_id: UUID, instrument_id: UUID) -> None:
        self.run = run
        self.account_id = account_id
        self.instrument_id = instrument_id

    async def _live_keys(self, txid: str) -> set[str]:
        t = models.Transaction
        rows = await self.run.session.scalars(
            select(t.external_id).where(
                t.connection_id == self.run.connection_id,
                t.tx_hash == txid,
                t.status != "void",
            )
        )
        return {key for key in rows if key is not None}

    async def _identifier(self, address: str) -> UUID:
        return await self.run.shared(
            ("chain_address", address),
            lambda cmd: get_or_create_identifier(cmd, "chain_address", address),
        )

    async def _post(self, stored: _Stored, movement: Movement) -> UUID:
        run, key = self.run, movement.key
        assert stored.block_time is not None  # confirmed
        quantity = _btc(movement.sats)
        own = PostingInput(self.account_id, self.instrument_id, quantity)
        counterparty: CounterpartyFields | None = None
        match movement.kind:
            case "receipt":
                contra = await run.contra_for(key, self.account_id, 0, None)
            case "payment":
                identifier_id = (
                    None if movement.address is None else await self._identifier(movement.address)
                )
                contra = await run.contra_for(key, self.account_id, 0, identifier_id)
                if identifier_id is not None:
                    counterparty = CounterpartyFields(
                        identifier_id=identifier_id, name=COUNTERPARTY_NAME
                    )
            case "fee":
                own = PostingInput(
                    self.account_id, self.instrument_id, quantity, is_fee=True, memo="fee"
                )
                contra = await run.system_account("unassigned")
                counterparty = CounterpartyFields(never_transfer=True)
            case "net":
                contra = await run.system_account("unassigned")
                counterparty = CounterpartyFields(never_transfer=True)
        unchanged = run.counts.unchanged
        posted = await run.post(
            key,
            stored.block_time,
            _DESCRIPTIONS[movement.kind],
            [own, PostingInput(contra, self.instrument_id, -quantity)],
            counterparty=counterparty,
            chain=ChainFields(CHAIN, stored.txid, movement.address),
        )
        if movement.kind == "net" and run.counts.unchanged == unchanged:
            run.flag(SHARED_FLAG)
        return posted

    async def apply(self, stored: _Stored) -> bool:
        """Bring the transaction's movements in line with its row; False, changing nothing,
        for a confirmed one whose block time is not known (a status read that gave none),
        which stays pending: its movements cannot be dated."""
        if stored.confirmed and stored.block_time is None:
            return False
        wanted = movements(stored.txid, stored.inputs, stored.outputs) if stored.confirmed else []
        posted = {m.key: await self._post(stored, m) for m in wanted}
        for key in sorted(await self._live_keys(stored.txid) - set(posted)):
            await self.run.void(key)
        await self._link(stored, wanted, posted)
        if not stored.confirmed and stored.normalized_confirmed:
            await self.run.session.execute(
                text(
                    "update finance.bitcoin_transactions set left_chain_at = :at"
                    " where connection_id = :c and txid = :t"
                ),
                {"at": datetime.now(UTC), "c": self.run.connection_id, "t": stored.txid},
            )
            self.run.flag(LEFT_CHAIN_FLAG)
        return True

    async def _link(self, stored: _Stored, wanted: list[Movement], posted: dict[str, UUID]) -> None:
        """Each typed row names the ledger transaction it posts now, or none."""
        by_vout = {m.vout: posted[m.key] for m in wanted if m.vout is not None}
        ids = {"c": self.run.connection_id, "t": stored.txid}
        for out in stored.outputs:
            await self.run.session.execute(
                text(
                    "update finance.bitcoin_outputs set transaction_id = :id"
                    " where connection_id = :c and txid = :t and vout = :v"
                    " and transaction_id is distinct from :id"
                ),
                ids | {"v": out.vout, "id": by_vout.get(out.vout)},
            )
        by_kind = {m.kind: posted[m.key] for m in wanted if m.kind in ("fee", "net")}
        await self.run.session.execute(
            text(
                "update finance.bitcoin_transactions"
                " set fee_transaction_id = :fee, net_transaction_id = :net"
                " where connection_id = :c and txid = :t"
                " and (fee_transaction_id is distinct from :fee"
                " or net_transaction_id is distinct from :net)"
            ),
            ids | {"fee": by_kind.get("fee"), "net": by_kind.get("net")},
        )


async def normalize(ctx: SyncContext, txids: Collection[str] | None) -> NormalizeReport:
    """Post the movements of the given stored transactions, or of every one (None, or a
    stored `normalizer_version` other than this one: the replay), and mark them normalized.

    In the sync's own transaction (`ctx.command`); the wallet's account must exist (the
    fetch makes it)."""
    session = ctx.command.session
    connection = await get_connection(session, ctx.connection.id)
    replay = txids is None or connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
    report = NormalizeReport()
    run = _Run(ctx, connection, report)
    recorded = connection.config.get(ACCOUNT_KEY)
    if not isinstance(recorded, str):
        raise DisplayableError("The Bitcoin wallet has no account yet.")
    normalizer = _Normalizer(run, UUID(recorded), await bitcoin(ctx, run.flag))
    stored = await _stored(run, None if replay else txids)
    waiting = {s.txid for s in stored if not await normalizer.apply(s)}
    given = {s.txid for s in stored} | set(txids or ())
    await mark_normalized(session, connection.id, given - waiting)
    if connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION:
        await update_config(
            ctx.command, connection.id, {NORMALIZER_VERSION_KEY: NORMALIZER_VERSION}
        )
    return report
