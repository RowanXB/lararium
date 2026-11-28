"""Reconciling a Bitcoin wallet against what its addresses report (design §5.6).

Each sync's scan reads every address's confirmed totals: the satoshis ever paid to it and the
satoshis ever spent from it. Their difference summed over the wallet is what the chain says
the wallet holds, a `reported` snapshot at the sync's time, never the account's balance (the
account is in `transactions` mode). The account's non-void postings that occurred at or
before that time must equal it to the satoshi; a difference means a transaction the history
did not return, a posting rule wrong for a shape no real data has shown, or a server that
answered inconsistently between the scan and the history. A missed address cannot be caught
here: the scan's gap rule is the only protection.

The warnings are what the latest check found, published as every connector's reconciliation
warnings are (connector_flags): replaced in full each check, the owner told of each text once,
a difference never corrected. A warning names the account, the day and the difference, never
an address or a transaction.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.timeutil import local_date
from lararium.modules.finance import connector_flags
from lararium.modules.finance.bitcoin.instruments import bitcoin
from lararium.modules.finance.bitcoin.parse import AddressStats
from lararium.modules.finance.connector_run import LONDON, differences, reported_snapshot
from lararium.modules.finance.money import to_plain_str

PLATFORM = "the Bitcoin blockchain"
_SATOSHI = Decimal(1).scaleb(-8)


@dataclass(frozen=True)
class Mismatch:
    account_name: str
    day: date
    difference: Decimal  # the ledger's less the chain's

    @property
    def warning(self) -> str:
        return (
            f"{self.account_name} BTC differs from {PLATFORM} on {self.day.isoformat()}"
            f" by {to_plain_str(self.difference)}"
        )


async def check(
    ctx: SyncContext,
    account_id: UUID,
    account_name: str,
    scanned: Mapping[str, AddressStats],
    at: datetime,
) -> list[Mismatch]:
    """Record the scanned addresses' confirmed balance as a `reported` snapshot at `at`, and
    return the difference from the account's posted balance then, if there is one. Runs in the
    sync's own transaction."""
    reported = (
        Decimal(sum(s.chain.funded_txo_sum - s.chain.spent_txo_sum for s in scanned.values()))
        * _SATOSHI
    )
    day = local_date(at, LONDON)
    instrument_id = await bitcoin(ctx)
    await reported_snapshot(ctx.command, account_id, instrument_id, reported, at, day)
    found = await differences(
        ctx.command.session, account_id, at, {instrument_id: reported}, only=[instrument_id]
    )
    # Whole satoshis, so a difference reads the same from one sync to the next (it is told once).
    return [Mismatch(account_name, day, difference.quantize(_SATOSHI)) for _, difference in found]


async def publish(ctx: SyncContext, mismatches: list[Mismatch] | None) -> dict[str, JSONValue]:
    """Replace the reconciliation warnings the connection last published with these, keeping
    every other warning; tell the owner, in one message, of each not told yet. None: no check
    this run, so the last check's warnings stand. Returns the cursor's keys to write."""
    return await connector_flags.publish_reconciliation(
        ctx,
        None if mismatches is None else [m.warning for m in mismatches],
        label="a Bitcoin wallet",
    )
