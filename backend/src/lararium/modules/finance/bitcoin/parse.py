"""Esplora responses, parsed fail-closed.

Every amount is a whole number of satoshis; a fraction, an exponent, a negative amount, a missing
field or a wrong type raises `MalformedResponse`, never a guess. The sync stores the amounts as
integers and checks them against the chain's own totals, so a value silently coerced here would
be a wrong balance there. An error names the endpoint and the field path (`vin[].prevout`),
never a value, an address or a transaction id.

A field the documented format does not list is not an error: `unknown_fields` names it so the
sync can flag it (mempool.space adds fields now and then, and none can change a parsed amount).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from lararium.core.errors import DisplayableError
from lararium.modules.finance.readonly_http import JSONValue

# Every bitcoin there will ever be, in satoshis: no amount can be more, and a larger one would
# overflow the typed tables' bigint columns.
MAX_SATOSHIS = 2_100_000_000_000_000
# The stats fields that are amounts (the others are counts).
_TXO_SUMS = frozenset({"funded_txo_sum", "spent_txo_sum"})
_TXO_STATS = (
    "tx_count",
    "funded_txo_count",
    "funded_txo_sum",
    "spent_txo_count",
    "spent_txo_sum",
)
_STATUS = ("confirmed", "block_height", "block_hash", "block_time")
_PREVOUT = (
    "scriptpubkey",
    "scriptpubkey_asm",
    "scriptpubkey_type",
    "scriptpubkey_address",
    "value",
)
_INPUT = (
    "txid",
    "vout",
    "is_coinbase",
    "scriptsig",
    "scriptsig_asm",
    "inner_redeemscript_asm",
    "inner_witnessscript_asm",
    "sequence",
    "witness",
    "prevout",
)
_TX = ("txid", "version", "locktime", "size", "weight", "sigops", "fee", "vin", "vout", "status")
_STATS = ("address", "chain_stats", "mempool_stats")


class MalformedResponse(DisplayableError):
    """The server answered with something that is not the documented format."""

    def __init__(self, endpoint: str, field: str) -> None:
        super().__init__(f"The Esplora {endpoint} response is malformed at {field}")


@dataclass(frozen=True)
class TxoStats:
    tx_count: int
    funded_txo_count: int
    funded_txo_sum: int
    spent_txo_count: int
    spent_txo_sum: int


@dataclass(frozen=True)
class AddressStats:
    address: str
    chain: TxoStats
    mempool: TxoStats


@dataclass(frozen=True)
class Status:
    confirmed: bool
    block_height: int | None
    block_hash: str | None
    block_time: datetime | None


@dataclass(frozen=True)
class Prevout:
    scriptpubkey: str
    scriptpubkey_asm: str
    scriptpubkey_type: str
    scriptpubkey_address: str | None
    value: int


# An output has the prevout's fields: the same script and value, seen from the spending side.
Output = Prevout


@dataclass(frozen=True)
class Input:
    txid: str
    vout: int
    is_coinbase: bool
    scriptsig: str
    scriptsig_asm: str
    inner_redeemscript_asm: str | None
    inner_witnessscript_asm: str | None
    sequence: int
    witness: tuple[str, ...]
    prevout: Prevout | None


@dataclass(frozen=True)
class Tx:
    txid: str
    version: int
    locktime: int
    size: int
    weight: int
    sigops: int | None
    fee: int
    vin: tuple[Input, ...]
    vout: tuple[Output, ...]
    status: Status


class _Reader:
    """Typed access to one object of one endpoint's response; `path` is the field path of the
    object, without list indexes."""

    def __init__(self, endpoint: str, path: str, value: JSONValue) -> None:
        self._endpoint = endpoint
        self._path = path
        if not isinstance(value, dict):
            raise self.bad("")
        self._obj = value

    def bad(self, key: str) -> MalformedResponse:
        return MalformedResponse(self._endpoint, f"{self._path}{key}" or "the body")

    def child(self, key: str, path: str | None = None) -> "_Reader":
        return _Reader(self._endpoint, f"{path or self._path + key}.", self._field(key))

    def _field(self, key: str) -> JSONValue:
        if key not in self._obj:
            raise self.bad(key)
        return self._obj[key]

    def has(self, key: str) -> bool:
        return self._obj.get(key) is not None

    def integer(self, key: str, *, minimum: int | None = 0) -> int:
        return as_int(self._field(key), self._endpoint, f"{self._path}{key}", minimum)

    def amount(self, key: str) -> int:
        """An amount in satoshis: a whole number from 0 to `MAX_SATOSHIS`."""
        found = self.integer(key)
        if found > MAX_SATOSHIS:
            raise self.bad(key)
        return found

    def optional_integer(self, key: str) -> int | None:
        return self.integer(key) if self.has(key) else None

    def string(self, key: str) -> str:
        value = self._field(key)
        if not isinstance(value, str):
            raise self.bad(key)
        return value

    def optional_string(self, key: str) -> str | None:
        return self.string(key) if self.has(key) else None

    def boolean(self, key: str) -> bool:
        value = self._field(key)
        if not isinstance(value, bool):
            raise self.bad(key)
        return value

    def items(self, key: str, *, required: bool = True) -> list[JSONValue]:
        if not required and not self.has(key):
            return []
        value = self._field(key)
        if not isinstance(value, list):
            raise self.bad(key)
        return value


def as_int(value: JSONValue, endpoint: str, field: str, minimum: int | None = 0) -> int:
    """`value` as an int: a whole number written without a fraction or exponent."""
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value.as_tuple().exponent != 0
        or (minimum is not None and value < minimum)
    ):
        raise MalformedResponse(endpoint, field)
    return int(value)


def _txo_stats(parent: _Reader, key: str) -> TxoStats:
    reader = parent.child(key)
    return TxoStats(
        *(reader.amount(name) if name in _TXO_SUMS else reader.integer(name) for name in _TXO_STATS)
    )


def parse_stats(body: JSONValue) -> AddressStats:
    reader = _Reader("address", "", body)
    return AddressStats(
        reader.string("address"),
        _txo_stats(reader, "chain_stats"),
        _txo_stats(reader, "mempool_stats"),
    )


def _status(reader: _Reader) -> Status:
    confirmed = reader.boolean("confirmed")
    if not confirmed:
        # Unconfirmed: the block fields are absent or null, never a half-confirmed state.
        for key in ("block_height", "block_hash", "block_time"):
            if reader.has(key):
                raise reader.bad(key)
        return Status(False, None, None, None)
    block_time = reader.optional_integer("block_time")
    try:
        when = None if block_time is None else datetime.fromtimestamp(block_time, UTC)
    except (OverflowError, OSError, ValueError):
        raise reader.bad("block_time") from None
    if not reader.has("block_height") or not reader.has("block_hash"):
        raise reader.bad("block_height" if not reader.has("block_height") else "block_hash")
    return Status(True, reader.integer("block_height"), reader.string("block_hash"), when)


def parse_status(body: JSONValue) -> Status:
    """A transaction's status: what `/tx/:txid/status` returns, which has no `block_time`."""
    return _status(_Reader("status", "", body))


def _prevout(reader: _Reader) -> Prevout:
    return Prevout(
        reader.string("scriptpubkey"),
        reader.string("scriptpubkey_asm"),
        reader.string("scriptpubkey_type"),
        reader.optional_string("scriptpubkey_address"),
        reader.amount("value"),
    )


def _input(reader: _Reader) -> Input:
    is_coinbase = reader.boolean("is_coinbase")
    prevout = None
    if reader.has("prevout"):
        prevout = _prevout(reader.child("prevout"))
    elif not is_coinbase:
        # The fee and the spent amount come from the prevout; without it the input's value is
        # unknown, so the transaction cannot be accepted.
        raise reader.bad("prevout")
    witness: list[str] = []
    for item in reader.items("witness", required=False):
        if not isinstance(item, str):
            raise reader.bad("witness")
        witness.append(item)
    return Input(
        reader.string("txid"),
        reader.integer("vout"),
        is_coinbase,
        reader.string("scriptsig"),
        reader.string("scriptsig_asm"),
        reader.optional_string("inner_redeemscript_asm"),
        reader.optional_string("inner_witnessscript_asm"),
        reader.integer("sequence"),
        tuple(witness),
        prevout,
    )


def _tx(value: JSONValue, endpoint: str, path: str) -> Tx:
    reader = _Reader(endpoint, path, value)
    vin = tuple(_input(_Reader(endpoint, f"{path}vin[].", item)) for item in reader.items("vin"))
    vout = tuple(
        _prevout(_Reader(endpoint, f"{path}vout[].", item)) for item in reader.items("vout")
    )
    return Tx(
        reader.string("txid"),
        reader.integer("version", minimum=None),
        reader.integer("locktime"),
        reader.integer("size"),
        reader.integer("weight"),
        reader.optional_integer("sigops"),
        reader.amount("fee"),
        vin,
        vout,
        _status(reader.child("status")),
    )


def parse_txs(body: JSONValue) -> list[Tx]:
    if not isinstance(body, list):
        raise MalformedResponse("txs", "the body")
    return [_tx(item, "txs", "") for item in body]


def parse_height(body: JSONValue) -> int:
    return as_int(body, "tip height", "the body")


def unknown_fields(body: JSONValue) -> set[str]:
    """The field paths in `body` the documented format does not list (`vout[].foo`), for a
    stats object, a transaction, a list of transactions or a status."""
    found: set[str] = set()
    if isinstance(body, list):
        for item in body:
            _unknown_tx(item, "", found)
    elif isinstance(body, dict):
        if "txid" in body:
            _unknown_tx(body, "", found)
        elif "chain_stats" in body or "mempool_stats" in body:
            _unknown(body, _STATS, "", found)
            for key in ("chain_stats", "mempool_stats"):
                _unknown(body.get(key), _TXO_STATS, f"{key}.", found)
        else:
            _unknown(body, _STATUS, "", found)
    return found


def _unknown(value: JSONValue | None, known: tuple[str, ...], path: str, found: set[str]) -> None:
    if isinstance(value, Mapping):
        found.update(f"{path}{key}" for key in value if key not in known)


def _unknown_tx(value: JSONValue, path: str, found: set[str]) -> None:
    if not isinstance(value, dict):
        return
    _unknown(value, _TX, path, found)
    _unknown(value.get("status"), _STATUS, f"{path}status.", found)
    for item in _list(value.get("vin")):
        _unknown(item, _INPUT, f"{path}vin[].", found)
        if isinstance(item, dict):
            _unknown(item.get("prevout"), _PREVOUT, f"{path}vin[].prevout.", found)
    for item in _list(value.get("vout")):
        _unknown(item, _PREVOUT, f"{path}vout[].", found)


def _list(value: JSONValue | None) -> list[JSONValue]:
    return value if isinstance(value, list) else []
