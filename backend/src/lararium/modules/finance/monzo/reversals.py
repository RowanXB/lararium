"""Which Monzo reversal cancels which card authorisation (card refunds design §4.3).

A merchant that reverses an authorisation makes Monzo send a second transaction, the
reversal, with the opposite amount; the authorisation itself never settles. A shared
lifecycle id is not enough on its own (one authorisation can clear as several settled
payments under one), so a reversal cancels only an authorisation it reverses exactly.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast


def _created(tx: dict[str, Any]) -> datetime | None:
    value = tx.get("created")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _amount(tx: dict[str, Any]) -> int | None:
    value = tx.get("amount")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _metadata(tx: dict[str, Any], key: str) -> object:
    metadata = tx.get("metadata")
    return cast(dict[str, Any], metadata).get(key) if isinstance(metadata, dict) else None


def _declined(tx: dict[str, Any]) -> bool:
    return tx.get("decline_reason") not in (None, "")


def _is_reversal(tx: dict[str, Any]) -> bool:
    amount = _amount(tx)
    return (
        _metadata(tx, "is_reversal") == "true"
        and amount is not None
        and amount > 0
        and tx.get("scheme") == "mastercard"
        and not _declined(tx)
    )


def reversal_pairs(copies: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Each reversal's Monzo id → the id of the authorisation it cancels, from raw Monzo
    transactions (the latest copy of each) and nothing else.

    A reversal has metadata.is_reversal "true", a positive amount and scheme mastercard, and
    no decline. Its authorisation has the same metadata.mastercard_lifecycle_id and
    currency, the opposite amount, an empty `settled`, no decline and no is_reversal, and was
    created before it. Reversals are taken by creation, then id; each takes the latest such
    authorisation (by creation, then id) that no earlier one took.
    """
    reversals: list[tuple[datetime, str, dict[str, Any]]] = []
    # By (lifecycle id, currency, amount): the authorisations, each with its creation.
    waiting: dict[tuple[object, object, int], list[tuple[datetime, str]]] = {}
    for tx in copies:
        created, amount = _created(tx), _amount(tx)
        lifecycle = _metadata(tx, "mastercard_lifecycle_id")
        if created is None or amount is None or not isinstance(lifecycle, str) or not lifecycle:
            continue
        if _is_reversal(tx):
            reversals.append((created, tx["id"], tx))
        elif (
            amount < 0
            and tx.get("settled") in (None, "")
            and not _declined(tx)
            and _metadata(tx, "is_reversal") != "true"
        ):
            waiting.setdefault((lifecycle, tx.get("currency"), amount), []).append(
                (created, tx["id"])
            )
    for authorisations in waiting.values():
        authorisations.sort()
    pairs: dict[str, str] = {}
    for created, reversal_id, tx in sorted(reversals, key=lambda r: (r[0], r[1])):
        key = (_metadata(tx, "mastercard_lifecycle_id"), tx.get("currency"), -tx["amount"])
        authorisations = waiting.get(key, [])
        earlier = [n for n, (at, _) in enumerate(authorisations) if at < created]
        if earlier:
            pairs[reversal_id] = authorisations.pop(earlier[-1])[1]
    return pairs
