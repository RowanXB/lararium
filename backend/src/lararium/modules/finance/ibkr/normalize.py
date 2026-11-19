"""IBKR raw records into the ledger. Filled in by the normalizer tasks; for now it books nothing."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from lararium.core.connectors import SyncContext


@dataclass(frozen=True)
class NormalizeReport:
    """What a normalize run changed."""


async def normalize(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], flagged: frozenset[str] = frozenset()
) -> NormalizeReport:
    del ctx, raw_record_ids, flagged
    return NormalizeReport()
