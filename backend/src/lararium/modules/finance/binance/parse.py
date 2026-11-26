"""Binance response bodies to validated rows, held to the catalogue's `FIELDS`.

A body is checked against its label's fields: a missing required field or a value of the
wrong JSON type is an error naming the field; a field the catalogue does not list, and a
value outside a field's documented values, are reported (by name; a value only when it is a
short upper-case code) and the body is still accepted, so Binance adding something never
stops a sync. Numbers in a body are `Decimal` (the client parses them so); nothing here
produces a binary fraction.
"""

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import cast

from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.modules.finance.binance.catalogue import ENDPOINTS, FIELDS, Field

# What `BinanceClient.call` returns: JSON, with every number that has a fraction a Decimal.
type ApiValue = bool | int | Decimal | str | list[ApiValue] | dict[str, ApiValue] | None
type Row = Mapping[str, ApiValue]

WILDCARD = "<>"
_SEGMENT = re.compile(r"^(?P<name>[^\[\]]*)(?P<steps>(?:\[\d*\])*)$")
_SHORT_CODE = re.compile(r"-?[A-Z0-9_]{1,32}")
OTHER_VALUE = "<other>"
# What an unknown field's name may be to appear in a flag.
_FLAG_PATH = re.compile(r"[A-Za-z0-9_.\[\]]{1,64}")


class BinanceDataError(DisplayableError):
    """A body that does not have the shape the catalogue gives; names the field, never a value."""

    def __init__(self, label: str, field: str) -> None:
        super().__init__(f"Binance answered {label} in an unexpected shape (field {field or '.'})")
        self.label = label
        self.field = field


@dataclass
class Node:
    """One place in a response body: its field (when the catalogue lists one) and what is below."""

    path: str
    field: Field | None = None
    # An object's keys; `WILDCARD` stands for any other key (a map).
    children: dict[str, "Node"] = dataclasses.field(default_factory=lambda: {})
    # An array's element.
    element: "Node | None" = None
    # Where this is an array's element, or a position in a bar (`[0]`).
    is_element: bool = False
    positional: bool = False

    def child(self, name: str) -> "Node":
        """The node of a key; a `<name>` stands for any key not listed."""
        key = WILDCARD if name.startswith("<") and name.endswith(">") else name
        if key not in self.children:
            self.children[key] = Node(f"{self.path}.{name}" if self.path else name)
        return self.children[key]

    def item(self) -> "Node":
        if self.element is None:
            self.element = Node(f"{self.path}[]", is_element=True)
        return self.element

    def position(self, index: int) -> "Node":
        self.positional = True
        if (name := f"[{index}]") not in self.children:
            self.children[name] = Node(f"{self.path}{name}")
        return self.children[name]


@cache
def schema(label: str) -> Node:
    """The tree of a label's fields, built from the catalogue."""
    root = Node("")
    for spec in FIELDS[label]:
        node = root
        for segment in spec.path.split("."):
            match = _SEGMENT.match(segment)
            assert match is not None, spec.path
            if name := match["name"]:
                node = node.child(name)
            for step in re.findall(r"\[\d*\]", match["steps"]):
                node = node.item() if step == "[]" else node.position(int(step[1:-1]))
        assert node.path == spec.path, spec.path
        node.field = spec
    return root


def as_api(value: JSONValue) -> ApiValue:
    """A stored answer (decimals as strings) as the parser takes it: every JSON value is one."""
    return cast(ApiValue, value)


@dataclass
class Parsed:
    # The records, as the endpoint's `rows` says; a bar of `klines` is a mapping from its
    # position (`"0"`, `"1"`, …) to the value.
    rows: list[Row]
    # The whole body, for what sits beside the rows (`total`, `uid`, an account's totals).
    body: ApiValue
    unknown_fields: set[str] = dataclasses.field(default_factory=lambda: set())
    # (field path, the value when it is a short upper-case code, else `OTHER_VALUE`)
    unknown_enums: set[tuple[str, str]] = dataclasses.field(default_factory=lambda: set())


def _is_int(value: ApiValue) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (
        isinstance(value, Decimal) and value.is_finite() and value == value.to_integral()
    )


def _is_decimal(value: ApiValue) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, str):
        try:
            return Decimal(value).is_finite()
        except InvalidOperation:
            return False
    return False


def _scalar_ok(kind: str, value: ApiValue) -> bool:
    match kind:
        case "decimal":
            return _is_decimal(value)
        case "integer" | "id" | "time_ms":
            return _is_int(value)
        case "boolean":
            return isinstance(value, bool)
        case "text" | "time_text":
            return isinstance(value, str)
        case _:
            raise AssertionError(kind)


def _shown(value: ApiValue) -> str:
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral() else str(value)
    return str(value)


class _Walk:
    def __init__(self, label: str, parsed: Parsed) -> None:
        self._label = label
        self._parsed = parsed

    def _fail(self, node: Node) -> BinanceDataError:
        return BinanceDataError(self._label, node.path)

    def walk(self, node: Node, value: ApiValue) -> None:
        spec = node.field
        if value is None:
            if spec is not None and spec.required:
                raise self._fail(node)
            return
        if spec is not None and spec.type not in ("object", "array"):
            self._scalar(node, spec, value)
        elif node.positional:
            self._tuple(node, value)
        elif node.element is not None or (
            spec is not None and spec.type == "array" and not node.is_element
        ):
            if not isinstance(value, list):
                raise self._fail(node)
            if node.element is not None:
                for item in value:
                    self.walk(node.element, item)
        else:
            self._object(node, value)

    def _scalar(self, node: Node, spec: Field, value: ApiValue) -> None:
        if not _scalar_ok(spec.type, value):
            raise self._fail(node)
        if spec.enum is not None and _shown(value) not in spec.enum:
            shown = _shown(value)
            code = shown if _SHORT_CODE.fullmatch(shown) else OTHER_VALUE
            self._parsed.unknown_enums.add((node.path, code))

    def _object(self, node: Node, value: ApiValue) -> None:
        if not isinstance(value, dict):
            raise self._fail(node)
        opaque = node.field is not None and node.field.opaque
        wildcard = node.children.get(WILDCARD)
        for key, item in value.items():
            child = node.children.get(key) or wildcard
            if child is not None:
                self.walk(child, item)
            elif not opaque:
                self._parsed.unknown_fields.add(f"{node.path}.{key}" if node.path else key)
        for name, child in node.children.items():
            if name != WILDCARD and child.field is not None and child.field.required:
                if value.get(name) is None:
                    raise self._fail(child)

    def _tuple(self, node: Node, value: ApiValue) -> None:
        if not isinstance(value, list):
            raise self._fail(node)
        for name, child in node.children.items():
            index = int(name[1:-1])
            if index >= len(value):
                if child.field is not None and child.field.required:
                    raise self._fail(child)
                continue
            self.walk(child, value[index])
        for index in range(len(node.children), len(value)):
            self._parsed.unknown_fields.add(f"{node.path}[{index}]")


def _says_there_are_records(envelope: Mapping[str, ApiValue]) -> bool:
    """Whether what sits beside a record list says it holds records: a `moreData` that is
    true, or a `total` above 0."""
    total = envelope.get("total")
    counted = isinstance(total, int | Decimal) and not isinstance(total, bool) and total > 0
    return envelope.get("moreData") is True or counted


def _rows(label: str, body: ApiValue) -> list[Row]:
    """The records of a body. A record list that is null or not a list (the body itself, for a
    label whose records are the body) is never an empty answer: it is a `BinanceDataError`; nor
    is a list left out of an answer that says it has records (`moreData`, `total`)."""
    where = ENDPOINTS[label].rows
    found: ApiValue = body
    if where not in ("", "."):
        *parents, last = where.split(".")
        for step in parents:
            found = found.get(step) if isinstance(found, dict) else None
        if not isinstance(found, dict):
            raise BinanceDataError(label, where)
        if last not in found:
            # Whether Binance leaves the list out of an empty window is not known, and the
            # real sync works so: no records. (Only the list itself: a missing parent is not.)
            # Not when the same answer says there are records: then the list is missing.
            if _says_there_are_records(found):
                raise BinanceDataError(label, where)
            return []
        found = found[last]
    elif where == "":
        found = [body]
    if not isinstance(found, list):
        raise BinanceDataError(label, where)
    rows: list[Row] = []
    for item in found:
        if isinstance(item, list):
            rows.append({str(index): value for index, value in enumerate(item)})
        elif isinstance(item, dict):
            rows.append(item)
        else:
            raise BinanceDataError(label, where)
    return rows


def parse(label: str, body: ApiValue) -> Parsed:
    """Check a body against the label's fields and pull out its records."""
    parsed = Parsed(rows=[], body=body)
    _Walk(label, parsed).walk(schema(label), body)
    parsed.rows = _rows(label, body)
    return parsed


def row_prefix(label: str) -> str:
    """The start of a path inside one of the label's records (`rows[].`)."""
    where = ENDPOINTS[label].rows
    return "[]." if where == "." else f"{where}[]."


def unknown_flags(label: str, parsed: Parsed) -> list[str]:
    """The flags for what the parser found unknown in an answer of the label: a field by its
    path from the record (Binance chose the name, so only a plain one is shown) and a value by
    its field and code. Never a value of a field."""
    prefix = row_prefix(label)
    flags: list[str] = []
    for path in sorted(parsed.unknown_fields):
        shown = path.removeprefix(prefix)
        shown = shown if _FLAG_PATH.fullmatch(shown) else "<other field>"
        flags.append(f"{label}.{shown} (a field the catalogue does not list)")
    for path, value in sorted(parsed.unknown_enums):
        flags.append(f"{label}.{path.removeprefix(prefix)}={value}")
    return flags
