"""Trading 212 normalizer: raw records into the ledger (Task 5 fills it in).

Versioned like IBKR's (NORMALIZER_VERSION, stored in the connection's config, Decision 8): a
new version replays every raw record the connection holds.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from lararium.core.connectors import SyncContext

NORMALIZER_VERSION = "trading212-norm-1"


@dataclass
class NormalizeReport:
    """What a normalize run changed, and what it could not map (IBKR's fields)."""

    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0
    # Ledger transactions.
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    flagged: set[str] = field(default_factory=set[str])
    mismatches: list[str] = field(default_factory=list[str])


async def normalize(ctx: SyncContext, raw_record_ids: Sequence[UUID]) -> NormalizeReport:
    """Apply raw records to the ledger. Nothing yet: the sync only stores them."""
    return NormalizeReport()
