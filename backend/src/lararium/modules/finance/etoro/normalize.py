"""eToro raw records into the ledger (design §5.5-5.11).

Deterministic from its inputs and versioned (NORMALIZER_VERSION, stored in the connection's
config, Decision 8). Task 10 maps the API's records (accounts, instruments, opens and closes,
eToro Money) and Task 11 the statement's; until then a run maps nothing and reports nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from lararium.core.connectors import SyncContext

__all__ = ["NORMALIZER_VERSION", "NormalizeReport", "normalize"]

NORMALIZER_VERSION = "etoro-norm-1"


@dataclass
class NormalizeReport:
    """What a normalize run changed, and what it could not map."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # Flags about the records the run normalized (fields, values, types it does not know).
    flagged: set[str] = field(default_factory=set[str])
    # The run replayed every stored record, so `flagged` is all there is to flag.
    replayed: bool = False


async def normalize(ctx: SyncContext, raw_record_ids: Sequence[UUID]) -> NormalizeReport:
    """Apply the raw records to the ledger: nothing yet (Tasks 10 and 11)."""
    return NormalizeReport()
