"""The eToro statement parser: sheets to keyed rows, its checks, and its errors' silence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lararium.modules.finance.etoro.fields import (
    ACCOUNT_ACTIVITY,
    ACCOUNT_SUMMARY,
    ACTIVITY_COLUMNS,
    CLOSED_COLUMNS,
    CLOSED_POSITIONS,
    DIVIDENDS,
    FINANCIAL_SUMMARY,
    STATEMENT_SHEETS,
)
from lararium.modules.finance.etoro.statement import (
    EtoroStatement,
    StatementError,
    StatementRow,
    parse_statement,
)

from .statement_fixtures import (
    ISIN,
    POSITION_1,
    POSITION_2,
    USERNAME,
    activity_row,
    build_statement,
    build_workbook,
    closed_row,
    dividend_row,
    statement_sheets,
    summary,
)

OPEN = activity_row(
    "02/10/2026 14:30:05",
    "Open Position",
    "500",
    details="ZZQA/USD",
    units="4",
    position_id=POSITION_1,
    change="0",
    asset_type="Stocks",
)
CLOSE = activity_row(
    "20/10/2026 15:01:44",
    "Position closed",
    "520.00",
    details="ZZQA/USD",
    units="4",
    position_id=POSITION_1,
    change="20.00",
    asset_type="Stocks",
)
TRANSFER_IN = activity_row("01/10/2026 10:00:00", "Transfer: GBP > USD", "1250.00", details="GBP")
OVERNIGHT = activity_row("03/10/2026 22:00:00", "Overnight fee", "-0.02", details="ZZQA/USD")
FINANCIAL = [
    {"Name": "Synthetic figure", "01/10/2026 - 31/10/2026": "12.40", "Year to date": "12.40"},
    {"Name": "Other figure", "01/10/2026 - 31/10/2026": "0.00", "Year to date": "0.00"},
]


# 0-9 to Arabic-Indic digits, which Python's \d and int() also accept.
ARABIC_INDIC = {ord(str(digit)): 0x0660 + digit for digit in range(10)}


def parse(body: bytes) -> EtoroStatement:
    return parse_statement(body, UTC)


def rows_of(statement: EtoroStatement, sheet: str) -> list[StatementRow]:
    return [row for row in statement.rows if row.sheet == sheet]


def test_parses_every_sheet() -> None:
    body = build_statement(
        summary(),
        [TRANSFER_IN, OPEN, CLOSE],
        [closed_row()],
        [dividend_row()],
        financial=FINANCIAL,
    )
    statement = parse(body)
    assert statement.period_from == datetime(2026, 10, 1, tzinfo=UTC)
    assert statement.period_to == datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    assert statement.created == datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    assert statement.username == USERNAME
    assert statement.currency == "USD"
    assert statement.flagged == frozenset()
    # The Glossary is not a data sheet; the others keep the workbook's order.
    assert [row.sheet for row in statement.rows] == (
        [ACCOUNT_SUMMARY] * 12
        + [CLOSED_POSITIONS]
        + [ACCOUNT_ACTIVITY] * 3
        + [DIVIDENDS]
        + [FINANCIAL_SUMMARY] * 2
    )

    labels = rows_of(statement, ACCOUNT_SUMMARY)
    # Every label and value is kept, including the ones without a value.
    assert labels[0].cells == {"Label": "Details", "Value": ""}
    assert {"Label": "Username", "Value": USERNAME} in [row.cells for row in labels]
    assert {"Label": "Transfer to Trading (from eToro Money)", "Value": "1250.00"} in [
        row.cells for row in labels
    ]

    (closed,) = rows_of(statement, CLOSED_POSITIONS)
    assert list(closed.cells) == list(CLOSED_COLUMNS)
    assert closed.cells["ISIN"] == ISIN and closed.cells["Notes"] == ""

    activity = rows_of(statement, ACCOUNT_ACTIVITY)
    assert [row.cells for row in activity] == [TRANSFER_IN, OPEN, CLOSE]
    assert list(activity[0].cells) == list(ACTIVITY_COLUMNS)

    (dividend,) = rows_of(statement, DIVIDENDS)
    assert dividend.cells == dividend_row()

    financial = rows_of(statement, FINANCIAL_SUMMARY)
    assert financial[0].cells == {**FINANCIAL[0], "Total": ""}
    assert [row.cells["Name"] for row in financial] == ["Synthetic figure", "Other figure"]

    keys = [row.key for row in statement.rows]
    assert len(set(keys)) == len(keys)
    assert all(len(key) == 32 and int(key, 16) >= 0 for key in keys)


def test_times_read_in_the_given_zone() -> None:
    zone = ZoneInfo("Europe/London")
    statement = parse_statement(build_statement(summary(start="01/07/2026 00:00:00"), []), zone)
    assert statement.period_from == datetime(2026, 7, 1, tzinfo=zone)
    assert statement.period_from.utcoffset() == timedelta(hours=1)


def test_key_is_decision_10() -> None:
    statement = parse(build_statement(summary(), [OPEN, OPEN], [closed_row()], [dividend_row()]))

    def key(*parts: str | int) -> str:
        text = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    first, second = rows_of(statement, ACCOUNT_ACTIVITY)
    expected = (ACCOUNT_ACTIVITY, OPEN["Date"], "Open Position", "ZZQA/USD", "500", "4")
    assert first.key == key(*expected, POSITION_1, 0)
    assert second.key == key(*expected, POSITION_1, 1)
    (closed,) = rows_of(statement, CLOSED_POSITIONS)
    c = closed_row()
    assert closed.key == key(
        CLOSED_POSITIONS,
        c["Position ID"],
        c["Open Date"],
        c["Close Date"],
        c["Units / Contracts"],
        c["Amount"],
        0,
    )
    (dividend,) = rows_of(statement, DIVIDENDS)
    d = dividend_row()
    assert dividend.key == key(
        DIVIDENDS, d["Date of Payment"], d["Position ID"], d["Net Dividend Received (USD)"], 0
    )


def test_summary_rows_keyed_by_period_and_label() -> None:
    def key(*parts: str | int) -> str:
        text = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    labels = summary()
    statement = parse(build_statement(labels, [], financial=FINANCIAL))
    period = (labels["Start Date"], labels["End Date"])
    deposits = next(
        row for row in rows_of(statement, ACCOUNT_SUMMARY) if row.cells["Label"] == "Deposits"
    )
    assert deposits.key == key(ACCOUNT_SUMMARY, *period, "Deposits", 0)
    figure = rows_of(statement, FINANCIAL_SUMMARY)[0]
    assert figure.key == key(FINANCIAL_SUMMARY, *period, "Synthetic figure", 0)

    # A corrected statement of the same period: changed figures keep their keys.
    corrected = parse(
        build_statement(
            {**labels, "Deposits": "5"},
            [],
            financial=[{**FINANCIAL[0], "Year to date": "99.99"}, FINANCIAL[1]],
        )
    )
    assert [row.key for row in corrected.rows] == [row.key for row in statement.rows]
    assert [row.cells for row in corrected.rows] != [row.cells for row in statement.rows]


def test_repeated_summary_label_gets_occurrence_keys() -> None:
    sheets = statement_sheets(summary(), [])
    name, rows = sheets[0]
    sheets[0] = (name, [*rows, [("shared", "Name"), ("inline", "Second Synthetic Name")]])
    names = [
        row
        for row in rows_of(parse(build_workbook(sheets)), ACCOUNT_SUMMARY)
        if row.cells["Label"] == "Name"
    ]
    assert len(names) == 2 and names[0].key != names[1].key


def test_identical_rows_get_occurrence_keys() -> None:
    statement = parse(build_statement(summary(), [OVERNIGHT, OPEN, OVERNIGHT]))
    first, _, third = rows_of(statement, ACCOUNT_ACTIVITY)
    assert first.cells == third.cells
    assert first.key != third.key
    # Only the occurrence differs: the third row's key is the first's with occurrence 1.
    same = parse(build_statement(summary(), [OVERNIGHT, OVERNIGHT]))
    assert [row.key for row in rows_of(same, ACCOUNT_ACTIVITY)] == [first.key, third.key]


def test_same_row_same_key_across_files() -> None:
    october = parse(
        build_statement(summary(), [TRANSFER_IN, OVERNIGHT, OPEN, OVERNIGHT, CLOSE], [closed_row()])
    )
    # An overlapping statement: a later start, a later end, other rows before and after, and
    # other running balances on the rows the two do not share.
    later = activity_row(
        "05/11/2026 09:00:00", "Transfer: USD > GBP", "-100.00", details="GBP", balance="5.00"
    )
    overlapping = parse(
        build_statement(
            summary(start="02/10/2026 00:00:00", end="30/11/2026 23:59:59"),
            [OVERNIGHT, OPEN, OVERNIGHT, CLOSE, later],
            [closed_row(POSITION_2), closed_row()],
        )
    )
    for sheet in (ACCOUNT_ACTIVITY, CLOSED_POSITIONS):
        keys = {row.key: row.cells for row in rows_of(october, sheet)}
        shared = {row.key: row.cells for row in rows_of(overlapping, sheet) if row.key in keys}
        assert shared == {k: v for k, v in keys.items() if v != TRANSFER_IN}, sheet
    # Summary rows are per statement: the same figure in two periods is two rows.
    october_summary = {row.key for row in rows_of(october, ACCOUNT_SUMMARY)}
    assert not october_summary & {row.key for row in rows_of(overlapping, ACCOUNT_SUMMARY)}
    # The same statement again gives the same keys throughout.
    again = parse(
        build_statement(summary(), [TRANSFER_IN, OVERNIGHT, OPEN, OVERNIGHT, CLOSE], [closed_row()])
    )
    assert [row.key for row in again.rows] == [row.key for row in october.rows]


def _without(columns: tuple[str, ...], dropped: str) -> list[str]:
    return [c for c in columns if c != dropped]


@pytest.mark.parametrize(
    ("sheet", "column"),
    [(sheet, column) for sheet, spec in STATEMENT_SHEETS.items() for column in spec.required],
)
def test_missing_required_column_names_it(sheet: str, column: str) -> None:
    sheets = statement_sheets(
        summary(), [OPEN], [closed_row()], [dividend_row()], financial=FINANCIAL
    )
    name, rows = next(s for s in sheets if s[0] == sheet)
    if STATEMENT_SHEETS[sheet].layout == "labels":
        kept = [row for row in rows if row[0] != ("shared", column)]
    else:
        drop = [cell for cell in rows[0]].index(("shared", column))
        kept = [[cell for i, cell in enumerate(row) if i != drop] for row in rows]
    sheets[sheets.index((name, rows))] = (name, kept)
    with pytest.raises(StatementError) as error:
        parse(build_workbook(sheets))
    assert f"{sheet}.{column}" in str(error.value)


@pytest.mark.parametrize("sheet", list(STATEMENT_SHEETS))
def test_missing_sheet_names_it(sheet: str) -> None:
    sheets = [s for s in statement_sheets(summary(), [OPEN]) if s[0] != sheet]
    with pytest.raises(StatementError, match=f"no {sheet} sheet"):
        parse(build_workbook(sheets))


def test_required_label_twice_refused() -> None:
    sheets = statement_sheets(summary(), [OPEN])
    name, rows = sheets[0]
    sheets[0] = (name, [*rows, [("shared", "Username"), ("inline", "synthetic-other")]])
    with pytest.raises(StatementError, match=r"Account Summary\.Username appears twice") as error:
        parse(build_workbook(sheets))
    assert "synthetic-other" not in str(error.value) and USERNAME not in str(error.value)


def test_column_twice_refused() -> None:
    sheets = statement_sheets(summary(), [OPEN])
    index = next(i for i, s in enumerate(sheets) if s[0] == ACCOUNT_ACTIVITY)
    name, rows = sheets[index]
    sheets[index] = (name, [[*row, row[1]] for row in rows])
    with pytest.raises(StatementError, match=r"Account Activity\.Type appears twice"):
        parse(build_workbook(sheets))


def test_non_ascii_digits_refused() -> None:
    # Arabic-Indic digits: a date only in another script's numerals is not eToro's format.
    arabic = "01/10/2026 00:00:00".translate(ARABIC_INDIC)
    with pytest.raises(StatementError, match=r"Start Date is not a date-time") as error:
        parse(build_statement(summary(start=arabic), []))
    assert arabic not in str(error.value)


def test_unknown_label_with_non_ascii_digits_only_not_flagged() -> None:
    sheets = statement_sheets(summary(), [])
    name, rows = sheets[0]
    sheets[0] = (name, [*rows, [("shared", "A heading"), ("inline", "12".translate(ARABIC_INDIC))]])
    assert parse(build_workbook(sheets)).flagged == frozenset()


def test_start_after_end_refused() -> None:
    body = build_statement(summary(start="02/11/2026 00:00:00"), [])
    with pytest.raises(StatementError, match="Start Date is after its End Date"):
        parse(body)


def test_extra_column_with_values_flagged() -> None:
    extra = {**OPEN, "Brand New Column": "x"}
    empty_extra = {**closed_row(), "Unused Column": ""}
    statement = parse(build_statement(summary(), [extra], [empty_extra]))
    assert statement.flagged == frozenset({"Account Activity.Brand New Column"})
    (row,) = rows_of(statement, ACCOUNT_ACTIVITY)
    assert row.cells["Brand New Column"] == "x"
    (closed,) = rows_of(statement, CLOSED_POSITIONS)
    assert closed.cells["Unused Column"] == ""
    # The extra column is not part of the key.
    assert row.key == rows_of(parse(build_statement(summary(), [OPEN])), ACCOUNT_ACTIVITY)[0].key


def test_cells_past_the_header_flagged_by_column_letter() -> None:
    sheets = statement_sheets(summary(), [OPEN])
    index = next(i for i, s in enumerate(sheets) if s[0] == ACCOUNT_ACTIVITY)
    name, rows = sheets[index]
    sheets[index] = (name, [rows[0], [*rows[1], ("inline", "stray")]])
    statement = parse(build_workbook(sheets))
    assert statement.flagged == frozenset({"Account Activity.column L"})
    assert rows_of(statement, ACCOUNT_ACTIVITY)[0].cells["column L"] == "stray"


def test_unknown_sheet_and_label_flagged_without_their_text() -> None:
    sheets = statement_sheets(summary(), [OPEN])
    name, rows = sheets[0]
    sheets[0] = (name, [*rows, [("shared", "A Label Nobody Knows"), ("inline", "9")]])
    sheets.append(("Extra Sheet", [[("inline", "something")]]))
    sheets.append(("Blank Sheet", []))
    statement = parse(build_workbook(sheets))
    assert statement.flagged == frozenset({"Extra Sheet", "Account Summary.<unknown label>"})
    assert {"Label": "A Label Nobody Knows", "Value": "9"} in [
        row.cells for row in rows_of(statement, ACCOUNT_SUMMARY)
    ]
    assert {row.sheet for row in statement.rows} == {ACCOUNT_SUMMARY, ACCOUNT_ACTIVITY}


def test_unknown_heading_without_figures_not_flagged() -> None:
    sheets = statement_sheets(summary(), [])
    name, rows = sheets[0]
    heading = [("shared", "Unrealized 2026"), ("shared", "Amount in USD"), ("shared", "Note")]
    sheets[0] = (name, [*rows, heading, [("shared", "A long disclaimer, unknown")]])
    statement = parse(build_workbook(sheets))
    assert statement.flagged == frozenset()
    assert {"Label": "Unrealized 2026", "Value": "Amount in USD", "Value 2": "Note"} in [
        row.cells for row in rows_of(statement, ACCOUNT_SUMMARY)
    ]


def test_wider_label_rows_keep_every_value() -> None:
    sheets = statement_sheets(summary(), [])
    name, rows = sheets[0]
    sheets[0] = (name, [*rows, [("shared", "Deposits"), ("number", "1"), ("number", "2")]])
    statement = parse(build_workbook(sheets))
    assert {"Label": "Deposits", "Value": "1", "Value 2": "2"} in [
        row.cells for row in rows_of(statement, ACCOUNT_SUMMARY)
    ]


def test_padding_to_a_wide_header_capped() -> None:
    # 1000 named columns and 101 one-cell rows: within the reader's cap, past it once padded.
    sheets = statement_sheets(summary(), [OPEN])
    index = next(i for i, s in enumerate(sheets) if s[0] == ACCOUNT_ACTIVITY)
    name, rows = sheets[index]
    header = [*rows[0], *(("shared", f"Wide {n}") for n in range(989))]
    sheets[index] = (name, [header, *([("number", "1")] for _ in range(101))])
    with pytest.raises(StatementError, match="Account Activity has more than 100000 cells"):
        parse(build_workbook(sheets))


def test_blank_rows_skipped() -> None:
    blank = {column: "" for column in ACTIVITY_COLUMNS}
    statement = parse(build_statement(summary(), [OPEN, blank, CLOSE]))
    assert [row.cells for row in rows_of(statement, ACCOUNT_ACTIVITY)] == [OPEN, CLOSE]


def test_xlsx_errors_are_statement_errors() -> None:
    with pytest.raises(StatementError, match="not an xlsx"):
        parse(b"not a zip")


PERSONAL = ["Synthetic Person", "synthetic-private-user", "SYNTHETIC-DETAIL-TEXT"]


@pytest.mark.parametrize(
    "case",
    ["bad date", "missing column", "missing label", "label twice", "empty value", "bad xml"],
)
def test_errors_never_contain_cell_values(case: str) -> None:
    labels = {**summary(username=PERSONAL[1]), "Name": PERSONAL[0]}
    row = {**OPEN, "Details": PERSONAL[2]}
    if case == "bad date":
        labels["Start Date"] = "2026-10-01 SYNTHETIC-DETAIL-TEXT"
    if case == "empty value":
        labels["Currency"] = ""
    sheets = statement_sheets(labels, [row], [closed_row()])
    name, rows = sheets[0]
    index = next(i for i, s in enumerate(sheets) if s[0] == ACCOUNT_ACTIVITY)
    if case == "missing column":
        activity = sheets[index][1]
        sheets[index] = (ACCOUNT_ACTIVITY, [r[:2] + r[3:] for r in activity])
    if case == "missing label":
        sheets[0] = (name, [r for r in rows if r[0] != ("shared", "End Date")])
    if case == "label twice":
        sheets[0] = (name, [*rows, [("shared", "Username"), ("inline", PERSONAL[1])]])
    body = build_workbook(sheets)
    if case == "bad xml":
        body = build_workbook([(ACCOUNT_SUMMARY, [[("inline", PERSONAL[0] + "\x01")]])])
    with pytest.raises(StatementError) as error:
        parse(body)
    message = str(error.value)
    # Every value long enough not to occur in a fixed message by chance.
    for value in [*PERSONAL, *labels.values(), *row.values()]:
        if len(value) >= 4:
            assert value not in message, (case, value)
