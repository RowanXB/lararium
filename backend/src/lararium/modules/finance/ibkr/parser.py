"""Reads a Flex statement into typed-by-section rows, checking it against the field lists.

Errors and flags name sections and fields, never values: Account Information and Transfers
hold personal data.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree
from xml.parsers import expat

from lararium.core.errors import DisplayableError
from lararium.modules.finance.ibkr.fields import (
    DERIVABLE_SECTIONS,
    EXECUTION_LEVEL,
    ROUTED_SECTIONS,
    SECTIONS,
    SINGLE_ROW_SECTIONS,
    TRADE_DETAIL_TAGS,
)

_SECTION_FIELD_LEVEL = "levelOfDetail"


class FlexDataError(DisplayableError, ValueError):
    """The statement does not match the Flex query; the message names a section and field only."""


@dataclass(frozen=True)
class Row:
    section: str
    account: str
    period_from: date
    period_to: date
    attributes: Mapping[str, str]


@dataclass(frozen=True)
class Statement:
    rows: tuple[Row, ...]
    # "<Section>.<attribute>", or "<Section>" for an unknown section that has rows.
    flagged: frozenset[str]
    # (accountId, container element) of every section a FlexStatement lists, empty or not:
    # an empty section says the account had nothing there, an absent one says nothing.
    sections: frozenset[tuple[str, str]] = frozenset()


def _refuse_doctype(*_: object) -> None:
    raise FlexDataError("the statement contains a DOCTYPE, which is refused")


def _root(body: bytes) -> ElementTree.Element:
    """Parse with expat directly: its DOCTYPE handler is how entity tricks are refused."""
    parser = expat.ParserCreate()
    stack: list[ElementTree.Element] = []
    roots: list[ElementTree.Element] = []

    def start(tag: str, attributes: dict[str, str]) -> None:
        element = ElementTree.Element(tag, attributes)
        if stack:
            stack[-1].append(element)
        else:
            roots.append(element)
        stack.append(element)

    def end(_tag: str) -> None:
        stack.pop()

    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(body, True)
    except expat.ExpatError:
        raise FlexDataError("the statement is not well-formed XML") from None
    return roots[0]


def _date(value: str | None, field: str) -> date:
    if value is None:
        raise FlexDataError(f"FlexStatement.{field} is missing: select it in the Flex query")
    try:
        return date.fromisoformat(value)  # accepts yyyyMMdd as well as yyyy-MM-dd
    except ValueError:
        pass
    raise FlexDataError(f"FlexStatement.{field} is not a date")


def _row(
    section: str,
    element: ElementTree.Element,
    account: str,
    period: tuple[date, date],
    flagged: set[str],
) -> Row | None:
    spec = SECTIONS[section]
    if spec.levels:
        level = element.get(_SECTION_FIELD_LEVEL)
        if level is None or not level.strip():
            raise FlexDataError(
                f"{section}.{_SECTION_FIELD_LEVEL} is missing: select it in the Flex query"
            )
        if level not in spec.levels:
            flagged.add(f"{section}.{_SECTION_FIELD_LEVEL}")
            return None
        if level != spec.level:
            return None
    # Only rows that are read are checked: a skipped SUMMARY row may carry another account.
    own_account = element.get("accountId")
    if own_account is not None and own_account.strip() and own_account != account:
        raise FlexDataError(f"{section}.accountId does not match its FlexStatement")
    attributes: dict[str, str] = {}
    for name, value in element.attrib.items():
        if name in spec.known:
            attributes[name] = value
        elif value != "":
            attributes[name] = value
            flagged.add(f"{section}.{name}")
    for name in sorted(spec.required):
        if not (element.get(name) or "").strip():
            raise FlexDataError(f"{section}.{name} is missing: select it in the Flex query")
    return Row(section, account, period[0], period[1], attributes)


def _section_of(container: str, element: ElementTree.Element) -> str | None:
    """The record type of a row element, which for Trade and Lot depends on its container."""
    tag = element.tag
    if container == "Trades":
        if tag == "Trade":
            if element.get(_SECTION_FIELD_LEVEL) == EXECUTION_LEVEL:
                return "Trade"
            return "TradeDetail"
        return "TradeDetail" if tag in TRADE_DETAIL_TAGS else None
    if container == "OpenPositions" and tag == "Lot":
        return "PositionLot"
    if tag in SECTIONS and tag not in ROUTED_SECTIONS:
        return tag
    return None


def _statement_rows(
    statement: ElementTree.Element, flagged: set[str], sections: set[tuple[str, str]]
) -> list[Row]:
    account = statement.get("accountId")
    if not account:
        raise FlexDataError("FlexStatement.accountId is missing: select it in the Flex query")
    period = (
        _date(statement.get("fromDate"), "fromDate"),
        _date(statement.get("toDate"), "toDate"),
    )
    found: list[Row] = []
    for container in statement:
        sections.add((account, container.tag))
        if container.tag in DERIVABLE_SECTIONS:
            continue
        if container.tag in SINGLE_ROW_SECTIONS:
            if container.attrib:
                row = _row(container.tag, container, account, period, flagged)
                if row is not None:
                    found.append(row)
            continue
        for child in container:
            section = _section_of(container.tag, child)
            if section is not None:
                row = _row(section, child, account, period, flagged)
                if row is not None:
                    found.append(row)
            else:
                flagged.add(child.tag)
        if not len(container) and container.attrib and container.tag not in SECTIONS:
            flagged.add(container.tag)
    return found


def parse_statement(body: bytes) -> Statement:
    root = _root(body)
    if root.tag != "FlexQueryResponse":
        raise FlexDataError("the file is not a Flex statement: its root is not FlexQueryResponse")
    statements = list(root.iter("FlexStatement"))
    if not statements:
        # Taken as an empty success, it would move the sync's cursor past days never read.
        raise FlexDataError("the Flex query returned no statements: select every account")
    flagged: set[str] = set()
    sections: set[tuple[str, str]] = set()
    merged: dict[tuple[object, ...], Row] = {}
    latest_info: dict[str, Row] = {}
    for statement in statements:
        for row in _statement_rows(statement, flagged, sections):
            if row.section == "AccountInformation":
                held = latest_info.get(row.account)
                if held is None or row.period_to >= held.period_to:
                    latest_info[row.account] = row
                continue
            if row.section == "ConversionRate":
                key: tuple[object, ...] = (
                    row.section,
                    row.attributes.get("reportDate"),
                    row.attributes.get("fromCurrency"),
                    row.attributes.get("toCurrency"),
                )
            else:
                key = (row.section, row.account, tuple(sorted(row.attributes.items())))
            held = merged.get(key)
            if held is None:
                merged[key] = row
            elif row.section != "ConversionRate":
                merged[key] = Row(
                    row.section,
                    row.account,
                    min(held.period_from, row.period_from),
                    max(held.period_to, row.period_to),
                    held.attributes,
                )
    rows = tuple(latest_info.values()) + tuple(merged.values())
    return Statement(rows, frozenset(flagged), frozenset(sections))
