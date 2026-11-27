"""Synthetic Esplora responses in the documented format, with invented hashes and addresses
derived from the BIP84 test vector (m/84'/0'/0' of "abandon ... about")."""

import copy
import json
from typing import Any

RECEIVING_0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
CHANGE_0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
# An output address that belongs to nobody in the tests.
OTHER = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
TXID_A = "aa" * 32
TXID_B = "bb" * 32
TXID_C = "cc" * 32
BLOCK_HASH = "dd" * 32


def txo_stats(**overrides: int) -> dict[str, int]:
    base = {
        "tx_count": 0,
        "funded_txo_count": 0,
        "funded_txo_sum": 0,
        "spent_txo_count": 0,
        "spent_txo_sum": 0,
    }
    return {**base, **overrides}


def stats(address: str = RECEIVING_0, **chain: int) -> dict[str, Any]:
    return {
        "address": address,
        "chain_stats": txo_stats(**chain),
        "mempool_stats": txo_stats(),
    }


def status(confirmed: bool = True) -> dict[str, Any]:
    if not confirmed:
        return {"confirmed": False}
    return {
        "confirmed": True,
        "block_height": 800_000,
        "block_hash": BLOCK_HASH,
        "block_time": 1_700_000_000,
    }


def tx(txid: str = TXID_A, confirmed: bool = True) -> dict[str, Any]:
    return {
        "txid": txid,
        "version": 2,
        "locktime": 0,
        "size": 222,
        "weight": 561,
        "sigops": 1,
        "fee": 141,
        "vin": [
            {
                "txid": TXID_B,
                "vout": 1,
                "is_coinbase": False,
                "scriptsig": "",
                "scriptsig_asm": "",
                "sequence": 4_294_967_293,
                "witness": ["30" * 36, "02" * 33],
                "prevout": {
                    "scriptpubkey": "0014" + "11" * 20,
                    "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 " + "11" * 20,
                    "scriptpubkey_type": "v0_p2wpkh",
                    "scriptpubkey_address": CHANGE_0,
                    "value": 100_000,
                },
            }
        ],
        "vout": [
            {
                "scriptpubkey": "0014" + "22" * 20,
                "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 " + "22" * 20,
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": RECEIVING_0,
                "value": 60_000,
            },
            {
                "scriptpubkey": "0014" + "33" * 20,
                "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 " + "33" * 20,
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": OTHER,
                "value": 39_859,
            },
        ],
        "status": status(confirmed),
    }


def coinbase_tx(txid: str = TXID_C) -> dict[str, Any]:
    built = tx(txid)
    built["vin"] = [
        {
            "txid": "00" * 32,
            "vout": 4_294_967_295,
            "is_coinbase": True,
            "scriptsig": "03" + "ab" * 3,
            "scriptsig_asm": "OP_PUSHBYTES_3 " + "ab" * 3,
            "sequence": 4_294_967_295,
            "prevout": None,
        }
    ]
    built["fee"] = 0
    return built


def body(value: Any) -> str:
    """The JSON text of `value`; a float written as `5.0` stays a float."""
    return json.dumps(value)


def without(value: dict[str, Any], *path: str | int) -> dict[str, Any]:
    """A deep copy of `value` with the key or item at `path` removed."""
    copied = copy.deepcopy(value)
    node: Any = copied
    for step in path[:-1]:
        node = node[step]
    del node[path[-1]]
    return copied
