"""A connector's flags (M6 Decision 4): what it stored but could not type or post, shown as
connection warnings `<prefix><reason>`, the owner told of each new one once; and, by the same
rules, its reconciliation warnings (Decision 12, publish_reconciliation).

The cursor keeps the warnings per part (`flags`: part → reasons) and the reasons the owner was
told (`notified`). A run decides the parts it checked; a part it did not check keeps the
warnings the cursor holds for it. A warning counts as told only once a send succeeds, so a
failed send is retried next run; one whose cause is gone clears without a word. A publisher
replaces exactly the warnings its cursor keys hold, never one it matches by its text alone.
"""

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

from lararium.core.connections import get_connection, set_warnings
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue

logger = logging.getLogger(__name__)

# Whether a warning's reason (the text after the prefix) is one this connector publishes, given
# the reasons the cursor keeps: those are replaced, every other warning is left to its writer.
type OwnReason = Callable[[str, Collection[str]], bool]


def _kept_flags(cursor: Mapping[str, JSONValue], key: str) -> dict[str, list[str]]:
    value = cursor.get(key)
    if not isinstance(value, dict):
        return {}
    return {
        part: [r for r in reasons if isinstance(r, str)]
        for part, reasons in value.items()
        if isinstance(reasons, list)
    }


def kept_reasons(reason: str, kept: Collection[str]) -> bool:
    """The plain rule: a connector's own reasons are those its cursor keeps."""
    return reason in kept


async def publish_flags(
    ctx: SyncContext,
    found: Iterable[tuple[str, str]],
    checked: Collection[str],
    *,
    cursor: Mapping[str, JSONValue] | None,
    prefix: str,
    label: str,
    own: OwnReason = kept_reasons,
    keys: tuple[str, str] = ("flags", "notified"),
    separator: str = ", ",
) -> dict[str, JSONValue]:
    """Replace the connector's flags among the connection's warnings with this run's, keeping
    every other warning as the connection stands now. `found`: (part, reason) of this run;
    `checked`: the parts it decided. `cursor`: the flags and notifications as the run began, or
    None to read them as the connection stands now (a run that is not a sync, or one that must
    see what an upload published meanwhile). `label` names the connector with its article ("an
    eToro"), for a failed send's log line. `keys`: the cursor's keys for the parts and the
    reasons told; `separator` joins the new ones in the message. Returns those two keys."""
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    if cursor is None:
        cursor = connection.cursor if isinstance(connection.cursor, dict) else {}
    parts_key, notified_key = keys
    before = _kept_flags(cursor, parts_key)
    parts: dict[str, set[str]] = {}
    for part, reason in found:
        parts.setdefault(part, set()).add(reason)
    for part, reasons in before.items():
        if part not in checked:
            parts.setdefault(part, set()).update(reasons)
    reasons = sorted({reason for part_reasons in parts.values() for reason in part_reasons})
    told_before = cursor.get(notified_key)
    told = set(told_before) if isinstance(told_before, list) else set[JSONValue]()
    notified = [reason for reason in reasons if reason in told]
    new = [reason for reason in reasons if reason not in told]
    if new:
        try:
            await ctx.notifier.send(f"{connection.display_name}: {prefix}{separator.join(new)}")
            notified = reasons
        except Exception as error:
            # The class only: the message could carry the notifier's webhook URL.
            logger.warning("sending %s notification failed (%s)", label, type(error).__name__)
    kept = {reason for old in before.values() for reason in old}
    previous = connection.warnings
    warnings = [
        w for w in previous if not (w.startswith(prefix) and own(w.removeprefix(prefix), kept))
    ] + [prefix + r for r in reasons]
    if warnings != previous:
        await set_warnings(ctx.command, connection.id, warnings)
    return {
        parts_key: {
            part: list[JSONValue](sorted(part_reasons))
            for part, part_reasons in sorted(parts.items())
        },
        notified_key: list[JSONValue](notified),
    }


# Reconciliation warnings (Decision 12) --------------------------------------------------------

# The cursor's keys for the reconciliation warnings a connector last published, and those told.
RECONCILIATION_KEYS = ("reconciliation", "reconciliation_notified")
# What the last check found, and the warning of a run that could not check.
_CHECKED = "checked"
_UNCHECKED = "unchecked"


async def publish_reconciliation(
    ctx: SyncContext,
    warnings: Sequence[str] | None,
    *,
    label: str,
    unchecked: str | None = None,
) -> dict[str, JSONValue]:
    """Replace the reconciliation warnings the connector published last (by its cursor, never
    by their text) with this check's `warnings`, keeping every other warning; tell the owner,
    in one message, of each not told yet. `warnings` None: the run could not check, so the last
    check's warnings stand, with `unchecked` added when given. Returns the cursor's keys
    (RECONCILIATION_KEYS), for the caller to write."""
    if warnings is None:
        found = [(_UNCHECKED, unchecked)] if unchecked else []
        checked = {_UNCHECKED}
    else:
        found = [(_CHECKED, warning) for warning in warnings]
        checked = {_CHECKED, _UNCHECKED}
    return await publish_flags(
        ctx,
        found,
        checked,
        cursor=None,
        prefix="",
        label=label,
        keys=RECONCILIATION_KEYS,
        separator="\n",
    )
