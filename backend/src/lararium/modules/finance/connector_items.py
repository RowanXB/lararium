"""Reading a connector's raw-record items (Trading 212's, eToro's): fields by dotted path, the
paths an item holds, and whether a required one is missing.

Raw records keep every number as its exact decimal text (readonly_http.record_json), so a
number is read from text. Errors are the connector's own class, and name the record type and
the field, never a value.
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.modules.finance.typed_tables import Value, parse_decimal, parse_moment

type Item = dict[str, JSONValue]
# A map's path (`…{}`) to the names its entries' key and value take in field paths.
type MapEntries = Mapping[str, tuple[str, str]]


def value_at(item: Item | None, path: str) -> JSONValue:
    value: JSONValue = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def text_at(item: Item | None, path: str) -> str:
    """The field as text; a number is its text (raw records keep numbers so)."""
    value = value_at(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value.strip() if isinstance(value, str) else ""


def object_at(item: Item | None, path: str) -> Item | None:
    value = value_at(item, path)
    return value if isinstance(value, dict) else None


def objects_at(item: Item | None, path: str) -> list[Item]:
    value = value_at(item, path)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def number_or_none(
    item: Item | None, path: str, label: str, error: type[DisplayableError]
) -> Decimal | None:
    """The field as a number; None when absent. `label` names the record type in errors."""
    value = value_at(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise error(f"{label} {path} is not a number")
    return parse_decimal(value, f"{label} {path}")


def moment_at(item: Item | None, path: str, label: str, error: type[DisplayableError]) -> datetime:
    """The field as a zoned instant; one missing, or without a zone, fails."""
    value = text_at(item, path)
    if not value:
        raise error(f"{label} lacks {path}")
    moment = parse_moment(value, f"{label} {path}")
    if moment.tzinfo is None:
        raise error(f"{label} {path} has no time zone")
    return moment


def flat(item: Item, prefix: str = "") -> dict[str, Value]:
    """The item's values by dotted path, arrays and maps left out: they are child rows."""
    found: dict[str, Value] = {}
    for key, value in item.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found |= flat(value, f"{path}.")
        elif isinstance(value, bool | str) or value is None:
            found[path] = value
        elif not isinstance(value, list):
            found[path] = str(value)
    return found


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def leaves(value: JSONValue, path: str = "", maps: MapEntries | None = None) -> Iterator[str]:
    """The paths of the item's values (the connector's field notation: `[]` an array's items,
    `{}` a map's, `maps` naming a map's key and value); null and empty ones have none."""
    if isinstance(value, dict):
        entries = (maps or {}).get(f"{path}{{}}")
        if entries is not None:
            key_field, value_field = entries
            for entry in value.values():
                yield f"{path}{{}}.{key_field}"
                yield from leaves(entry, f"{path}{{}}.{value_field}", maps)
            return
        for key, entry in value.items():
            yield from leaves(entry, _join(path, key), maps)
    elif isinstance(value, list):
        for entry in value:
            yield from leaves(entry, f"{path}[]", maps)
    elif value is not None:
        yield path


def lacks(
    container: JSONValue, parts: Sequence[str], maps: MapEntries | None = None, path: str = ""
) -> bool:
    """Whether a required path (split at its dots) is missing below `container`: one under an
    array or a map only where that array or map is there."""
    head, rest = parts[0], parts[1:]
    name = head.removesuffix("[]").removesuffix("{}")
    here = _join(path, head)
    value = container.get(name) if isinstance(container, dict) else None
    if head.endswith("[]"):
        if value is None:
            return False
        if not isinstance(value, list):
            return True
        return any(e is None if not rest else lacks(e, rest, maps, here) for e in value)
    if head.endswith("{}"):
        if value is None:
            return False
        key_field, value_field = (maps or {})[here]
        if not isinstance(value, dict) or not rest or rest[0] not in (key_field, value_field):
            return True
        if rest[0] == key_field:
            return False
        below = rest[1:]
        return any(v is None if not below else lacks(v, below, maps, here) for v in value.values())
    if not rest:
        return value is None
    if value is None:
        # A parent missing: the path is missing, unless it lies in an array or map.
        return not any(p.endswith(("[]", "{}")) for p in rest)
    return lacks(value, rest, maps, here)
