"""The Bitcoin wallet connector (design 5.1, 5.2, 5.9): one connection is one wallet.

A sync runs at most every `SYNC_EVERY`, unless the owner asked for it: transactions post only
once confirmed, blocks come about every ten minutes, and the server is a public one. Each sync
builds the wallet from its credential and the config's gap limits, fetches (which commits as
it goes, so it must run before the connection's row is locked here), then, holding that lock,
posts what changed, checks the balance against the addresses' reported sums and publishes
the flags.

The credential (purpose `descriptor`) is the owner's input as JSON (`input_json`): a master
public key reveals every address the wallet will ever use, so it is kept encrypted. The config
holds what is not secret: `form`, `electrum_type`, `gaps` (`{"<source>": {"0": n, "1": n}}`)
and `fingerprint`, and what the fetch and the normalizer record there.

The flags are connection warnings `Bitcoin flagged: <reason>`, the owner told of each once
(connector_flags), kept per part:

- `addresses`: an address held by another account, and a scan that found no used address
  (most likely the wrong wallet type, so it stands until an address is used); decided by
  every sync's scan.
- `fee`: a spend whose fee does not add up; it stays pending, so every sync sees it again.
- `fields`: the response fields the documented format does not list, as each response that
  carries one is fetched (a history only when its address changed), so they accumulate.
- `events`: what the normalizer saw happen once (a shared transaction, a confirmed one that
  left the chain, several BTC instruments to merge), which no later run sees again, so
  they accumulate too.

No flag, error or log line names an address, a key or a transaction.
"""

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from lararium.core.connections import load_credential, lock_connection
from lararium.core.connectors import Capability, SyncContext
from lararium.core.errors import CredentialRejectedError, DisplayableError
from lararium.core.json_types import JSONValue
from lararium.modules.finance import connector_flags
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.bitcoin import reconcile
from lararium.modules.finance.bitcoin.accounts import ACCOUNT_KEY
from lararium.modules.finance.bitcoin.client import EsploraClient
from lararium.modules.finance.bitcoin.derive import Branch, Gaps, build_wallet
from lararium.modules.finance.bitcoin.fetch import VERSION, FetchResult, fetch
from lararium.modules.finance.bitcoin.instruments import SEVERAL_FLAG
from lararium.modules.finance.bitcoin.keys import WalletInput, parse_input
from lararium.modules.finance.bitcoin.normalize import (
    FEE_FLAG,
    LEFT_CHAIN_FLAG,
    NORMALIZER_VERSION,
    NORMALIZER_VERSION_KEY,
    SHARED_FLAG,
    normalize,
)
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.readonly_http import Clock, Sleep, monotonic

DESCRIPTOR_PURPOSE = "descriptor"
SYNC_EVERY = timedelta(hours=6)
SYNCED_AT_KEY = "synced_at"
GAPS_KEY = "gaps"
FLAGGED_PREFIX = "Bitcoin flagged: "

CONFLICT_FLAG = "address held by another account"
NO_USED_FLAG = "no used address found"
UNKNOWN_FIELD = "unknown field "

ADDRESSES = "addresses"
FEE = "fee"
FIELDS = "fields"
EVENTS = "events"
# The parts every sync decides in full; the others keep what earlier syncs found.
CHECKED = frozenset({ADDRESSES, FEE})
# The normalizer's flags by part; one it raises that is not here is an event.
_NORMALIZER_PARTS = {
    FEE_FLAG: FEE,
    SHARED_FLAG: EVENTS,
    LEFT_CHAIN_FLAG: EVENTS,
    SEVERAL_FLAG: EVENTS,
}

_INPUT_FIELDS = ("form", "electrum_type", "keys", "threshold", "descriptors", "addresses")
_UNREADABLE = "The Bitcoin wallet's stored keys or addresses could not be read"
_GAPS_UNREADABLE = "The Bitcoin wallet's gap limits could not be read"
_BRANCHES: dict[str, Branch] = {"0": 0, "1": 1}


class BitcoinSyncError(DisplayableError):
    """The sync cannot go on; the message names what to fix, never a value."""


def input_json(spec: WalletInput) -> str:
    """The credential's text: the owner's input, every field present."""
    return json.dumps(
        {
            "form": spec.form,
            "electrum_type": spec.electrum_type,
            "keys": list(spec.keys),
            "threshold": spec.threshold,
            "descriptors": list(spec.descriptors),
            "addresses": list(spec.addresses),
        }
    )


def _texts(value: JSONValue) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError
    texts = tuple(v for v in value if isinstance(v, str))
    if len(texts) != len(value):
        raise ValueError
    return texts


def load_input(stored: str) -> WalletInput:
    """The input `input_json` wrote; ValueError for anything else. Not validated: the caller
    runs `parse_input`."""
    value: JSONValue = json.loads(stored)
    if not isinstance(value, dict) or set(value) != set(_INPUT_FIELDS):
        raise ValueError
    form, kind, threshold = value["form"], value["electrum_type"], value["threshold"]
    if form not in ("electrum", "descriptor", "addresses"):
        raise ValueError
    if kind is not None and kind not in ("single", "multisig", "2fa", "old"):
        raise ValueError
    if threshold is not None and (not isinstance(threshold, int) or isinstance(threshold, bool)):
        raise ValueError
    return WalletInput(
        form,
        kind,
        _texts(value["keys"]),
        threshold,
        _texts(value["descriptors"]),
        _texts(value["addresses"]),
    )


def _gaps(config: Mapping[str, JSONValue]) -> Gaps | None:
    """The config's gap limits with branch numbers for keys; None (the defaults) when absent."""
    stored = config.get(GAPS_KEY)
    if stored is None:
        return None
    if not isinstance(stored, dict):
        raise BitcoinSyncError(_GAPS_UNREADABLE)
    gaps: dict[str, dict[Branch, int]] = {}
    for source, branches in stored.items():
        if not isinstance(branches, dict):
            raise BitcoinSyncError(_GAPS_UNREADABLE)
        gaps[source] = {}
        for branch, limit in branches.items():
            if branch not in _BRANCHES or not isinstance(limit, int) or isinstance(limit, bool):
                raise BitcoinSyncError(_GAPS_UNREADABLE)
            gaps[source][_BRANCHES[branch]] = limit
    return gaps


def _due(cursor: JSONValue, now: datetime) -> bool:
    synced_at = cursor.get(SYNCED_AT_KEY) if isinstance(cursor, dict) else None
    if not isinstance(synced_at, str):
        return True
    try:
        last = datetime.fromisoformat(synced_at)
    except ValueError:
        return True
    return last.tzinfo is None or now - last >= SYNC_EVERY


def _found(result: FetchResult, normalizer_flags: Iterable[str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if result.conflicts:
        found.add((ADDRESSES, CONFLICT_FLAG))
    if not any(s.chain.tx_count or s.mempool.tx_count for s in result.scanned.values()):
        found.add((ADDRESSES, NO_USED_FLAG))
    found |= {(FIELDS, UNKNOWN_FIELD + path) for path in result.unknown}
    found |= {(_NORMALIZER_PARTS.get(flag, EVENTS), flag) for flag in normalizer_flags}
    return found


async def _checked_at(ctx: SyncContext, now: datetime) -> datetime:
    """When the balance check holds: now, or the latest confirmed block's time when that is
    later (a block may be stamped up to about two hours ahead), so every posted movement is
    on or before it."""
    latest = await ctx.command.session.scalar(
        text(
            "select max(block_time) from finance.bitcoin_transactions"
            " where connection_id = :c and confirmed"
        ),
        {"c": ctx.connection.id},
    )
    return now if latest is None else max(now, latest)


class BitcoinConnector:
    name = "bitcoin"
    version = VERSION
    capabilities: frozenset[Capability] = frozenset({"balances", "transactions"})

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._clock = clock

    async def check_access(self, ctx: SyncContext) -> bool:
        # The chain is public: there is no secret for a provider to accept.
        return True

    async def _wallet_input(self, ctx: SyncContext) -> WalletInput:
        session = ctx.command.session
        stored = await load_credential(
            session, ctx.master_key, ctx.connection.id, DESCRIPTOR_PURPOSE
        )
        if stored is None:
            raise CredentialRejectedError(_UNREADABLE)
        try:
            return parse_input(load_input(stored.reveal()))
        except (ValueError, FinanceValidationError):
            # FinanceValidationError's message may describe the input: never repeated.
            raise CredentialRejectedError(_UNREADABLE) from None

    async def sync(self, ctx: SyncContext) -> JSONValue:
        connection = ctx.connection
        now = self._now()
        if not ctx.requested and not _due(connection.cursor, now):
            return connection.cursor
        spec = await self._wallet_input(ctx)
        wallet = build_wallet(spec, _gaps(connection.config))
        client = EsploraClient(ctx.http, ctx.esplora_url, sleep=self._sleep, clock=self._clock)
        result = await fetch(ctx, client, wallet)

        # Locked from here until the sync commits. Nothing below writes the connection's row
        # through `ctx.session_factory`: that would wait on this lock.
        locked = await lock_connection(ctx.command, connection.id)
        replay = locked.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
        report = await normalize(ctx, None if replay else result.changed_txids | result.left_chain)

        recorded = locked.config.get(ACCOUNT_KEY)
        assert isinstance(recorded, str), "the fetch made the wallet's account"
        account_id = UUID(recorded)
        account = await get_account(ctx.command.session, account_id)
        at = await _checked_at(ctx, now)
        mismatches = await reconcile.check(ctx, account_id, account.name, result.scanned, at)
        reconciled = await reconcile.publish(ctx, mismatches)

        # BTC prices (design 5.7) are updated here, their flags published with these.
        flags = await connector_flags.publish_flags(
            ctx,
            _found(result, report.flagged),
            CHECKED,
            cursor=None,
            prefix=FLAGGED_PREFIX,
            label="a Bitcoin wallet",
        )
        after: dict[str, JSONValue] = dict(locked.cursor) if isinstance(locked.cursor, dict) else {}
        return after | reconciled | flags | {SYNCED_AT_KEY: now.isoformat()}
