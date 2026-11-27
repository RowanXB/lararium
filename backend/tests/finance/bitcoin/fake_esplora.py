"""An in-memory Bitcoin chain served as Esplora's HTTP API through `httpx.MockTransport`.

Transactions are invented: their ids are counters, their scripts are made up from the address
they pay, and an input's previous transaction need not exist. The address stats are computed
from the transactions, so they always agree with the histories, as a real server's do.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any

import httpx

from lararium.modules.finance.bitcoin.client import EsploraClient
from lararium.modules.finance.readonly_http import HttpRetryableError

BASE = "https://esplora.test/api"
TIP = 800_100
PAGE = 25  # confirmed transactions per history page
MEMPOOL_PAGE = 50
# Nobody's address in these tests: a BIP84 test vector address of another key.
STRANGER = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def txid(n: int) -> str:
    return f"{n:064x}"


def block_hash(height: int, fork: int = 0) -> str:
    return hashlib.sha256(f"{height}:{fork}".encode()).hexdigest()


def block_time(height: int) -> int:
    return 1_600_000_000 + height * 600


def _script(address: str | None) -> dict[str, Any]:
    program = hashlib.sha256((address or "nobody").encode()).hexdigest()[:40]
    if address is None:
        return {
            "scriptpubkey": "6a04" + program[:8],
            "scriptpubkey_asm": "OP_RETURN OP_PUSHBYTES_4 " + program[:8],
            "scriptpubkey_type": "op_return",
        }
    return {
        "scriptpubkey": "0014" + program,
        "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 " + program,
        "scriptpubkey_type": "v0_p2wpkh",
        "scriptpubkey_address": address,
    }


def output(address: str | None, value: int) -> dict[str, Any]:
    return {**_script(address), "value": value}


def make_tx(
    n: int,
    pays: list[tuple[str | None, int]],
    *,
    spends: list[tuple[str, int]] | None = None,
    height: int | None = TIP - 100,
    fee: int = 141,
) -> dict[str, Any]:
    """Transaction `n`: inputs spending `spends` (address, value; a stranger's by default),
    outputs paying `pays`, confirmed at `height` (None: in the mempool)."""
    spends = spends if spends is not None else [(STRANGER, sum(v for _, v in pays) + fee)]
    vin = [
        {
            "txid": txid(1_000_000 + n * 10 + i),
            "vout": i,
            "is_coinbase": False,
            "scriptsig": "",
            "scriptsig_asm": "",
            "sequence": 4_294_967_293,
            "witness": ["30" * 36, f"{i:02x}" * 33],
            "prevout": output(address, value),
        }
        for i, (address, value) in enumerate(spends)
    ]
    return {
        "txid": txid(n),
        "version": 2,
        "locktime": 0,
        "size": 100 + 50 * len(vin) + 30 * len(pays),
        "weight": 400 + 200 * len(vin) + 120 * len(pays),
        "sigops": len(vin),
        "fee": fee,
        "vin": vin,
        "vout": [output(address, value) for address, value in pays],
        "status": _status(height),
    }


def _status(height: int | None, fork: int = 0) -> dict[str, Any]:
    if height is None:
        return {"confirmed": False}
    return {
        "confirmed": True,
        "block_height": height,
        "block_hash": block_hash(height, fork),
        "block_time": block_time(height),
    }


def _touches(tx: dict[str, Any], address: str) -> bool:
    return any(o.get("scriptpubkey_address") == address for o in tx["vout"]) or any(
        (i["prevout"] or {}).get("scriptpubkey_address") == address for i in tx["vin"]
    )


class FakeEsplora:
    def __init__(self, tip: int = TIP) -> None:
        self.tip = tip
        self.txs: list[dict[str, Any]] = []  # in arrival order
        self.requests: list[str] = []  # paths under the API's base
        # Raise HttpRetryableError when this many address requests have been answered.
        self.fail_after_stats: int | None = None
        self.now = Decimal(1000)

    # Pacing: a fake clock that sleeping advances.
    async def sleep(self, seconds: int) -> None:
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def client(self, http: httpx.AsyncClient) -> EsploraClient:
        return EsploraClient(http, BASE, sleep=self.sleep, clock=self.clock)

    # The chain.
    def add(self, tx: dict[str, Any]) -> dict[str, Any]:
        self.txs.append(tx)
        return tx

    def find(self, wanted: str) -> dict[str, Any]:
        return next(tx for tx in self.txs if tx["txid"] == wanted)

    def move(self, wanted: str, height: int | None, fork: int = 1) -> None:
        """Put the transaction in another block, or back in the mempool (None)."""
        self.find(wanted)["status"] = _status(height, fork)

    def drop(self, wanted: str) -> None:
        """The server forgets the transaction: gone from every history, its status a 404."""
        self.txs.remove(self.find(wanted))

    def history(self, address: str) -> list[dict[str, Any]]:
        """Newest first: the mempool, then the confirmed by height."""
        touching = [(n, tx) for n, tx in enumerate(self.txs) if _touches(tx, address)]
        mempool = [tx for n, tx in reversed(touching) if not tx["status"]["confirmed"]]
        confirmed = sorted(
            (
                (tx["status"]["block_height"], n, tx)
                for n, tx in touching
                if tx["status"]["confirmed"]
            ),
            key=lambda item: (item[0], item[1]),
            reverse=True,
        )
        return mempool + [tx for _, _, tx in confirmed]

    def stats(self, address: str) -> dict[str, Any]:
        def txo(confirmed: bool) -> dict[str, int]:
            found = {
                "tx_count": 0,
                "funded_txo_count": 0,
                "funded_txo_sum": 0,
                "spent_txo_count": 0,
                "spent_txo_sum": 0,
            }
            for tx in self.txs:
                if tx["status"]["confirmed"] != confirmed or not _touches(tx, address):
                    continue
                found["tx_count"] += 1
                for o in tx["vout"]:
                    if o.get("scriptpubkey_address") == address:
                        found["funded_txo_count"] += 1
                        found["funded_txo_sum"] += o["value"]
                for i in tx["vin"]:
                    if (i["prevout"] or {}).get("scriptpubkey_address") == address:
                        found["spent_txo_count"] += 1
                        found["spent_txo_sum"] += i["prevout"]["value"]
            return found

        return {"address": address, "chain_stats": txo(True), "mempool_stats": txo(False)}

    # The API.
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        parts = path.strip("/").split("/")
        if parts[0] == "address" and len(parts) == 2 and self.fail_after_stats is not None:
            if sum(1 for p in self.requests if p.count("/") == 2) >= self.fail_after_stats:
                raise HttpRetryableError("The API could not be reached")
        self.requests.append(path)
        if parts == ["blocks", "tip", "height"]:
            return httpx.Response(200, text=str(self.tip))
        if parts[0] == "address" and len(parts) == 2:
            return self._json(self.stats(parts[1]))
        if parts[0] == "address" and parts[2:] == ["txs"]:
            history = self.history(parts[1])
            mempool = [tx for tx in history if not tx["status"]["confirmed"]][:MEMPOOL_PAGE]
            confirmed = [tx for tx in history if tx["status"]["confirmed"]][:PAGE]
            return self._json(mempool + confirmed)
        if parts[0] == "address" and parts[2:4] == ["txs", "chain"] and len(parts) == 5:
            confirmed = [tx for tx in self.history(parts[1]) if tx["status"]["confirmed"]]
            after = next(n for n, tx in enumerate(confirmed) if tx["txid"] == parts[4])
            return self._json(confirmed[after + 1 : after + 1 + PAGE])
        if parts[0] == "tx" and parts[2:] == ["status"]:
            found = [tx for tx in self.txs if tx["txid"] == parts[1]]
            if not found:
                return httpx.Response(404, text="Transaction not found")
            return self._json(found[0]["status"])
        return httpx.Response(404, text="unknown path")

    @staticmethod
    def _json(value: Any) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(value))

    # What was asked.
    def asked_stats(self) -> list[str]:
        return [
            p.split("/")[2]
            for p in self.requests
            if p.startswith("/address/") and p.count("/") == 2
        ]

    def asked_txs(self) -> list[str]:
        return [p for p in self.requests if p.startswith("/address/") and "/txs" in p]

    def asked_status(self) -> list[str]:
        return [p.split("/")[2] for p in self.requests if p.startswith("/tx/")]
