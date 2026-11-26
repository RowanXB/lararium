"""Synthetic eToro account statements (xlsx): invented usernames, position ids, ISINs, amounts
and dates only.

`workbook_parts` writes the parts of a valid xlsx (shared strings, inline strings, numbers,
booleans), `zip_parts` zips them, so a test can tamper with one part. `build_statement` lays
out the six sheets as eToro does: labels and values in Account Summary, a header row then data
rows elsewhere. Headers and labels are shared strings, other text inline strings, numbers
numbers.
"""

import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from typing import Literal
from xml.sax.saxutils import escape, quoteattr

from lararium.modules.finance.etoro.fields import (
    ACCOUNT_ACTIVITY,
    ACCOUNT_SUMMARY,
    ACTIVITY_COLUMNS,
    CLOSED_COLUMNS,
    CLOSED_POSITIONS,
    DIVIDEND_COLUMNS,
    DIVIDENDS,
    FINANCIAL_SUMMARY,
    GLOSSARY,
)
from tests.finance.trading212.fixtures import isin

type Kind = Literal["shared", "rich", "inline", "number", "bool", "formula"]
# A plain string is a number when it looks like one, else a shared string; None leaves a gap.
type Cell = str | tuple[Kind, str] | None
type SheetSpec = tuple[str, Sequence[Sequence[Cell]]]

USERNAME = "synthetic-trader-07"
ISIN = isin("XS00ZZQA000")
ISIN_2 = isin("XS00ZZQB000")
POSITION_1 = "3000000001"
POSITION_2 = "3000000002"

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _kind(cell: str | tuple[Kind, str]) -> tuple[Kind, str]:
    if isinstance(cell, tuple):
        return cell
    return ("number" if _NUMBER.fullmatch(cell) else "shared", cell)


def _text(value: str) -> str:
    return f'<t xml:space="preserve">{escape(value)}</t>'


def _sheet_xml(rows: Sequence[Sequence[Cell]], shared: list[str]) -> str:
    out: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c, cell in enumerate(row):
            if cell is None:
                continue
            ref = f"{column_letter(c)}{r}"
            kind, value = _kind(cell)
            if kind in ("shared", "rich"):
                shared.append(f"{kind}:{value}")
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            elif kind == "inline":
                cells.append(f'<c r="{ref}" t="inlineStr"><is>{_text(value)}</is></c>')
            elif kind == "bool":
                cells.append(f'<c r="{ref}" t="b"><v>{value}</v></c>')
            elif kind == "formula":
                cells.append(f'<c r="{ref}" t="str"><f>CONCAT("x")</f><v>{escape(value)}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    return (
        f'{_DECL}<worksheet xmlns="{_MAIN}" xmlns:r="{_DOC_RELS}">'
        f"<sheetData>{''.join(out)}</sheetData></worksheet>"
    )


def _shared_xml(shared: list[str]) -> str:
    items: list[str] = []
    for entry in shared:
        kind, value = entry.split(":", 1)
        if kind == "rich":
            # Two runs and a phonetic hint, which is not part of the text.
            half = len(value) // 2
            items.append(
                f"<si><r><rPr><b/></rPr>{_text(value[:half])}</r><r>{_text(value[half:])}</r>"
                f'<rPh sb="0" eb="1"><t>PHONETIC</t></rPh></si>'
            )
        else:
            items.append(f"<si>{_text(value)}</si>")
    return (
        f'{_DECL}<sst xmlns="{_MAIN}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        f"{''.join(items)}</sst>"
    )


def workbook_parts(sheets: Sequence[SheetSpec]) -> dict[str, bytes]:
    """The parts of an xlsx holding `sheets` in this order. Sheet parts are numbered in reverse,
    so a reader that took the zip's order instead of the workbook's would get them backwards."""
    shared: list[str] = []
    parts: dict[str, str] = {}
    entries: list[str] = []
    relationships: list[str] = []
    count = len(sheets)
    for position, (name, rows) in enumerate(sheets, start=1):
        number = count - position + 1
        parts[f"xl/worksheets/sheet{number}.xml"] = _sheet_xml(rows, shared)
        entries.append(f'<sheet name={quoteattr(name)} sheetId="{position}" r:id="rId{number}"/>')
        relationships.append(
            f'<Relationship Id="rId{number}" Type="{_DOC_RELS}/worksheet" '
            f'Target="worksheets/sheet{number}.xml"/>'
        )
    relationships.append(
        f'<Relationship Id="rIdS" Type="{_DOC_RELS}/sharedStrings" Target="sharedStrings.xml"/>'
    )
    relationships.append(f'<Relationship Id="rIdT" Type="{_DOC_RELS}/styles" Target="styles.xml"/>')
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="application/'
        f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for n in range(1, count + 1)
    )
    parts["[Content_Types].xml"] = (
        f'{_DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )
    parts["_rels/.rels"] = (
        f'{_DECL}<Relationships xmlns="{_PKG_RELS}">'
        f'<Relationship Id="rId1" Type="{_DOC_RELS}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    parts["xl/workbook.xml"] = (
        f'{_DECL}<workbook xmlns="{_MAIN}" xmlns:r="{_DOC_RELS}">'
        f"<sheets>{''.join(entries)}</sheets></workbook>"
    )
    parts["xl/_rels/workbook.xml.rels"] = (
        f'{_DECL}<Relationships xmlns="{_PKG_RELS}">{"".join(relationships)}</Relationships>'
    )
    parts["xl/sharedStrings.xml"] = _shared_xml(shared)
    parts["xl/styles.xml"] = (
        f'{_DECL}<styleSheet xmlns="{_MAIN}"><fonts count="1"><font/></fonts>'
        '<fills count="1"><fill/></fills><borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="1"><xf/></cellXfs>'
        "</styleSheet>"
    )
    return {name: body.encode() for name, body in parts.items()}


def zip_parts(parts: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def build_workbook(sheets: Sequence[SheetSpec]) -> bytes:
    return zip_parts(workbook_parts(sheets))


def summary(
    *,
    start: str = "01/10/2026 00:00:00",
    end: str = "31/10/2026 23:59:59",
    created: str = "01/10/2026 09:15:00",
    username: str = USERNAME,
    currency: str = "USD/GBP",
) -> dict[str, str]:
    return {
        "Name": "Synthetic Trader",
        "Username": username,
        "Currency": currency,
        "Date Created": created,
        "Start Date": start,
        "End Date": end,
        "Beginning Realized Equity": "0",
        "Deposits": "0",
        "Transfer to Trading (from eToro Money)": "1250.00",
        "Ending Realized Equity": "1262.40",
    }


def activity_row(
    date: str,
    type_: str,
    amount: str,
    *,
    details: str = "-",
    units: str = "-",
    position_id: str = "-",
    change: str | None = None,
    equity: str = "1000.00",
    balance: str = "1000.00",
    asset_type: str = "-",
) -> dict[str, str]:
    return {
        "Date": date,
        "Type": type_,
        "Details": details,
        "Amount": amount,
        "Units / Contracts": units,
        "Realized Equity Change": amount if change is None else change,
        "Realized Equity": equity,
        "Balance": balance,
        "Position ID": position_id,
        "Asset type": asset_type,
        "NWA": "0",
    }


def closed_row(position_id: str = POSITION_1, isin_code: str = ISIN) -> dict[str, str]:
    return {
        "Position ID": position_id,
        "Action": "Buy ZZQA Synthetic",
        "Long / Short": "Long",
        "Amount": "500",
        "Units / Contracts": "4",
        "Open Date": "02/10/2026 14:30:05",
        "Close Date": "20/10/2026 15:01:44",
        "Leverage": "1",
        "Spread Fees (USD)": "0",
        "Market Spread (USD)": "-0.42",
        "Profit(USD)": "20.00",
        "Profit(GBP)": "15.2",
        "FX rate at open (USD)": "1",
        "FX rate at close (USD)": "1",
        "Open Rate": "125",
        "Close Rate": "130.0",
        "Take profit rate": "0",
        "Stop loss rate": "0",
        "Overnight Fees and Dividends": "0",
        "Copied From": "-",
        "Type": "Stocks",
        "ISIN": isin_code,
        "Notes": "",
    }


def dividend_row(position_id: str = POSITION_1) -> dict[str, str]:
    return {
        "Date of Payment": "15/10/2026",
        "Instrument Name": "ZZQA Synthetic",
        "Net Dividend Received (USD)": "0.85",
        "Net dividends": "0.85",
        "Currency": "USD",
        "Franked/Unfranked": "-",
        "Franking Credits (AUD)": "0",
        "Net Dividend Received (GBP)": "0.64",
        "Withholding Tax Rate (%)": "15",
        "Withholding Tax Amount (USD)": "0.15",
        "Withholding Tax Amount (GBP)": "0.11",
        "Position ID": position_id,
        "Type": "Stocks",
        "ISIN": ISIN,
    }


def _data(value: str) -> Cell:
    if value == "":
        return None
    return ("number", value) if _NUMBER.fullmatch(value) else ("inline", value)


def _table(columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> list[list[Cell]]:
    """A header row of `columns` plus any other key the rows have, then the rows."""
    header = list(columns)
    for row in rows:
        header.extend(k for k in row if k not in header)
    out: list[list[Cell]] = [[("shared", name) for name in header]]
    out.extend([_data(row.get(name, "")) for name in header] for row in rows)
    return out


def statement_sheets(
    summary: Mapping[str, str],
    activity: Sequence[Mapping[str, str]],
    closed: Sequence[Mapping[str, str]] = (),
    dividends: Sequence[Mapping[str, str]] = (),
    *,
    financial: Sequence[Mapping[str, str]] = (),
) -> list[SheetSpec]:
    summary_rows: list[list[Cell]] = [[("shared", "Details")]]
    summary_rows.extend([("shared", label), _data(value)] for label, value in summary.items())
    summary_rows.append([("shared", "Values shown in brackets indicate negative amounts")])
    financial_columns = ("Name", "01/10/2026 - 31/10/2026", "Year to date", "Total")
    return [
        (ACCOUNT_SUMMARY, summary_rows),
        (CLOSED_POSITIONS, _table(CLOSED_COLUMNS, closed)),
        (ACCOUNT_ACTIVITY, _table(ACTIVITY_COLUMNS, activity)),
        (DIVIDENDS, _table(DIVIDEND_COLUMNS, dividends)),
        (FINANCIAL_SUMMARY, _table(financial_columns, financial)),
        (GLOSSARY, [[("shared", "Account Statement Glossary")], ["Realized Equity", "Synthetic"]]),
    ]


def build_statement(
    summary: Mapping[str, str],
    activity: Sequence[Mapping[str, str]],
    closed: Sequence[Mapping[str, str]] = (),
    dividends: Sequence[Mapping[str, str]] = (),
    *,
    financial: Sequence[Mapping[str, str]] = (),
) -> bytes:
    """A valid synthetic eToro statement."""
    return build_workbook(
        statement_sheets(summary, activity, closed, dividends, financial=financial)
    )
