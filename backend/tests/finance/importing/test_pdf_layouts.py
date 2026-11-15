import re
from datetime import date
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    ParsedStatement,
    StatementAccount,
    StatementRate,
)
from lararium.modules.finance.importing.pdf import PARSER_VERSION, parse_pdf
from lararium.modules.finance.importing.profiles import PdfProfileConfig

from .synthetic_pdfs import (
    BocRow,
    BocStatement,
    HsbcRow,
    HsbcStatement,
    LloydsRow,
    LloydsStatement,
    boc_pdf,
    encrypted_pdf,
    hsbc_pdf,
    lloyds_pdf,
    scanned_pdf,
)

LONDON = ZoneInfo("Europe/London")
type Layout = Literal["hsbc_uk_statement", "lloyds_statement", "boc_uk_statement"]


def _parse(content: bytes, layout: Layout) -> ParsedStatement:
    return parse_pdf(content, PdfProfileConfig(format="pdf", layout=layout), LONDON)


def _rows(statement: ParsedStatement) -> list[ParsedRow]:
    rows = [row for row in statement.rows if isinstance(row, ParsedRow)]
    assert len(rows) == len(statement.rows)
    return rows


def _hsbc(statement: HsbcStatement) -> ParsedStatement:
    return _parse(hsbc_pdf(statement), "hsbc_uk_statement")


def _lloyds(statement: LloydsStatement) -> ParsedStatement:
    return _parse(lloyds_pdf(statement), "lloyds_statement")


def _boc(statement: BocStatement) -> ParsedStatement:
    return _parse(boc_pdf(statement), "boc_uk_statement")


THREE_ROWS = [
    HsbcRow(
        "VIS", ("EXAMPLE CAFE", "EXAMPLETOWN"), paid_out="4.20", date="08 May 27", balance="95.80"
    ),
    HsbcRow("CR", ("EXAMPLE EMPLOYER",), paid_in="1,250.00", date="10 May 27"),
    HsbcRow(")))", ("EXAMPLE SHOP",), paid_out="17.35", balance="1,328.45"),
]


def test_parser_version() -> None:
    assert PARSER_VERSION == "pdf-1"


# HSBC UK -------------------------------------------------------------------------------


def test_hsbc_rows_and_summary() -> None:
    statement = _hsbc(HsbcStatement(pages=[THREE_ROWS]))

    rows = _rows(statement)
    assert [(row.position, row.booking_date, row.quantity, row.bank_type) for row in rows] == [
        (0, date(2027, 5, 8), Decimal("-4.20"), "VIS"),
        (1, date(2027, 5, 10), Decimal("1250.00"), "CR"),
        (2, date(2027, 5, 10), Decimal("-17.35"), ")))"),
    ]
    assert rows[0].description == "EXAMPLE CAFE EXAMPLETOWN"
    assert rows[0].raw == {
        "Date": "08 May 27",
        "Payment type": "VIS",
        "Details": "EXAMPLE CAFE EXAMPLETOWN",
        "Paid out": "4.20",
        "Balance": "95.80",
    }
    assert all(row.currency == "GBP" and row.occurred_at is None for row in rows)
    assert all(row.fee == 0 and row.counterparty is None for row in rows)
    assert statement.period == (date(2027, 5, 6), date(2027, 6, 5))
    assert statement.opening_balance == Decimal("100.00")
    assert statement.money_in == Decimal("1250.00")
    assert statement.money_out == Decimal("21.55")
    assert statement.closing_balance == Decimal("1328.45")
    assert statement.closing_balance_date == date(2027, 6, 5)


def test_hsbc_date_carried_and_day_end_balance() -> None:
    rows = _rows(_hsbc(HsbcStatement(pages=[THREE_ROWS])))

    assert rows[2].booking_date == date(2027, 5, 10)  # printed only on the day's first row
    assert [row.balance for row in rows] == [Decimal("95.80"), None, Decimal("1328.45")]
    assert "Date" in rows[2].raw and "Balance" not in rows[1].raw


def test_hsbc_overdrawn_balance() -> None:
    statement = _hsbc(
        HsbcStatement(
            opening="1.50",
            pages=[
                [
                    HsbcRow(
                        "VIS",
                        ("EXAMPLE APP STORE",),
                        paid_out="4.75",
                        date="10 May 27",
                        balance="3.25 D",
                    ),
                    HsbcRow(
                        "CR", ("A EXAMPLE",), paid_in="30.00", date="12 May 27", balance="26.75"
                    ),
                ]
            ],
        )
    )

    rows = _rows(statement)
    assert [row.balance for row in rows] == [Decimal("-3.25"), Decimal("26.75")]
    assert rows[0].raw["Balance"] == "3.25 D"


def test_hsbc_multipage_continuation() -> None:
    statement = _hsbc(
        HsbcStatement(
            pages=[
                [
                    HsbcRow(
                        "VIS", ("EXAMPLE CAFE",), paid_out="4.20", date="08 May 27", balance="95.80"
                    )
                ],
                [
                    HsbcRow("BP", ("A EXAMPLE", "Transfer"), paid_out="6.50", date="12 May 27"),
                ],
                [
                    HsbcRow(")))", ("EXAMPLE SHOP",), paid_out="10.00", balance="79.30"),
                    HsbcRow(
                        "DD",
                        ("EXAMPLE ENERGY",),
                        paid_out="30.00",
                        date="02 Jun 27",
                        balance="49.30",
                    ),
                ],
            ]
        )
    )

    rows = _rows(statement)
    assert [(row.booking_date, row.balance) for row in rows] == [
        (date(2027, 5, 8), Decimal("95.80")),
        (date(2027, 5, 12), None),
        (date(2027, 5, 12), Decimal("79.30")),  # the day continues on the next page
        (date(2027, 6, 2), Decimal("49.30")),
    ]
    assert rows[1].description == "A EXAMPLE Transfer"
    assert statement.closing_balance == Decimal("49.30")


def test_hsbc_first_statement_without_brought_forward() -> None:
    statement = _hsbc(
        HsbcStatement(
            opening="0.00",
            brought_forward_date=None,
            pages=[
                [
                    HsbcRow(
                        "CR",
                        ("CASH IN AT EXAMPLE BANK",),
                        paid_in="50.00",
                        date="24 May 27",
                        balance="50.00",
                    )
                ]
            ],
        )
    )

    assert statement.opening_balance == Decimal("0.00")
    assert [row.quantity for row in _rows(statement)] == [Decimal("50.00")]


def test_hsbc_multiline_description() -> None:
    # The details column is set tight: words are a couple of points apart with no space.
    statement = _hsbc(
        HsbcStatement(
            pages=[
                [
                    HsbcRow(
                        "VIS",
                        ("INT'L 0012345678", "EXAMPLE.COM/BILL", "USD 12.00 @ 1.2500"),
                        paid_out="9.60",
                        date="08 May 27",
                        balance="90.40",
                    )
                ]
            ]
        )
    )

    row = _rows(statement)[0]
    assert row.description == "INT'L 0012345678 EXAMPLE.COM/BILL USD 12.00 @ 1.2500"
    assert row.quantity == Decimal("-9.60")


def test_hsbc_rate_table() -> None:
    statement = _hsbc(HsbcStatement(pages=[THREE_ROWS], credit_rate=True))

    assert statement.rates == [
        StatementRate(
            kind="Credit Interest Rates",
            band_from=None,
            band_to=Decimal("5000"),
            rate=Decimal("1.25"),
            ear=None,
        ),
        StatementRate(
            kind="Overdraft Interest Rates",
            band_from=None,
            band_to=Decimal("50"),
            rate=None,
            ear=Decimal("0.00"),
        ),
        StatementRate(
            kind="Overdraft Interest Rates",
            band_from=Decimal("50"),
            band_to=None,
            rate=None,
            ear=Decimal("35.00"),
        ),
    ]


def test_hsbc_credit_interest_not_paid() -> None:
    statement = _hsbc(HsbcStatement(pages=[THREE_ROWS]))

    assert [rate.kind for rate in statement.rates] == ["Overdraft Interest Rates"] * 2


def test_hsbc_period_rolled_back_to_29_february_refused() -> None:
    # The start's year is the end's, minus one when the start is later in the year; a
    # 29 February start rolled back into a common year is not a date.
    content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], period="29 February to 28 January 2028"))

    with pytest.raises(FileFormatError, match="the period's start is not a date"):
        _parse(content, "hsbc_uk_statement")


def test_hsbc_period_across_new_year() -> None:
    statement = _hsbc(
        HsbcStatement(
            pages=[
                [HsbcRow("CR", ("EXAMPLE",), paid_in="5.00", date="02 Jan 27", balance="105.00")]
            ],
            period="20 December to 19 January 2027",
            brought_forward_date="19 Dec 26",
            closing_day="19 Jan 27",
        )
    )

    assert statement.period == (date(2026, 12, 20), date(2027, 1, 19))


def test_hsbc_rate_tier_after_the_table_refused() -> None:
    # The table ends at the first gap of more than 20 pt; a tier below it isn't dropped.
    content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], late_tier=True))

    with pytest.raises(FileFormatError, match="rate table on page 1 has a tier below its end"):
        _parse(content, "hsbc_uk_statement")


# Lloyds --------------------------------------------------------------------------------


LLOYDS_ROWS = [
    LloydsRow("07 Jun 27", ("CASH IN AT EXAMPLE", "BRANCH"), "DEP", money_in="65.00"),
    LloydsRow("21 Jun 27", ("A EXAMPLE",), "FPO", money_out="7.25"),
]


def test_lloyds_rows_and_summary() -> None:
    statement = _lloyds(LloydsStatement(rows=LLOYDS_ROWS))

    rows = _rows(statement)
    assert [
        (row.booking_date, row.description, row.quantity, row.balance, row.bank_type)
        for row in rows
    ] == [
        (
            date(2027, 6, 7),
            "CASH IN AT EXAMPLE BRANCH",
            Decimal("65.00"),
            Decimal("75.00"),
            "DEP",
        ),
        (date(2027, 6, 21), "A EXAMPLE", Decimal("-7.25"), Decimal("67.75"), "FPO"),
    ]
    assert rows[1].raw == {
        "Date": "21 Jun 27",
        "Description": "A EXAMPLE",
        "Type": "FPO",
        "Money Out (£)": "7.25",
        "Balance (£)": "67.75",
    }
    assert statement.period == (date(2027, 6, 1), date(2027, 6, 30))
    assert statement.opening_balance == Decimal("10.00")
    assert statement.money_in == Decimal("65.00")
    assert statement.money_out == Decimal("7.25")
    assert statement.closing_balance == Decimal("67.75")
    assert statement.closing_balance_date == date(2027, 6, 30)


def test_lloyds_hidden_layer_ignored() -> None:
    rows = _rows(_lloyds(LloydsStatement(rows=LLOYDS_ROWS)))

    for row in rows:
        assert "blank" not in row.description
        assert "Column" not in row.description
        assert "Date" not in row.description and "Money" not in row.description
        assert all("blank" not in value for value in row.raw.values())


def test_lloyds_takes_one_type_code() -> None:
    # A second word in the type column is not part of the type code.
    content = lloyds_pdf(LloydsStatement(rows=LLOYDS_ROWS, extra=[(0, 272.0, 352.0, "XX")]))

    with pytest.raises(FileFormatError, match="words outside any column on page 1"):
        _parse(content, "lloyds_statement")


# BOC UK --------------------------------------------------------------------------------


LOCAL_OUT = BocRow(
    "03/05/27",
    "03/05/27",
    (
        "/PTG2I02600000001//Local",
        "Clearing And Outward",
        "Remittance Alex Example",
        "401234 12345678 EXAMPLE",
        "BANK PLC Online Banking",
        "rent",
    ),
    paid_out="140.00",
)
INTERNATIONAL_IN = BocRow(
    "11/05/27",
    "12/05/27",
    (
        "/TI27051100000001//Internat",
        "ional Inward Remittance",
        "EXAMPLE SENDER REFED-",
        "NO:",
        "PTG1S02600000001BUSIN-",
        "NO:TI27051100000001",
        "/PURP/OTHR///URI/HOUSE",
        "HOLD GOODS",
    ),
    paid_in="61.37",
)
TRANSFER_IN = BocRow(
    "24/05/27",
    "24/05/27",
    ("///Inward Transfer A", "EXAMPLE 51234 Example", "Bank (UK) Limited, Example", "Branch"),
    paid_in="19.45",
)


def test_boc_rows_summary_and_counts() -> None:
    statement = _boc(BocStatement(pages=[[LOCAL_OUT, INTERNATIONAL_IN], [TRANSFER_IN]]))

    rows = _rows(statement)
    assert [(row.booking_date, row.quantity, row.balance) for row in rows] == [
        (date(2027, 5, 3), Decimal("-140.00"), Decimal("0.00")),
        (date(2027, 5, 11), Decimal("61.37"), Decimal("61.37")),
        (date(2027, 5, 24), Decimal("19.45"), Decimal("80.82")),
    ]
    assert statement.payments_out == 1
    assert statement.payments_in == 2
    assert statement.money_in == Decimal("80.82")
    assert statement.money_out == Decimal("140.00")
    assert statement.opening_balance == Decimal("140.00")
    assert statement.closing_balance == Decimal("80.82")
    assert statement.period == (date(2027, 5, 1), date(2027, 5, 31))


def test_boc_payment_counts_checked() -> None:
    content = boc_pdf(
        BocStatement(pages=[[LOCAL_OUT, INTERNATIONAL_IN]], summary={"in_volume": "2"})
    )

    with pytest.raises(FileFormatError, match="2 payments in"):
        _parse(content, "boc_uk_statement")


def test_boc_multiline_details() -> None:
    rows = _rows(_boc(BocStatement(pages=[[LOCAL_OUT, INTERNATIONAL_IN]])))

    local, international = rows
    assert local.description == (
        "/PTG2I02600000001//Local Clearing And Outward Remittance Alex Example "
        "401234 12345678 EXAMPLE BANK PLC Online Banking rent"
    )
    assert local.value_date == date(2027, 5, 3)
    assert local.counterparty == Counterparty(
        name="Alex Example",
        sort_code="401234",
        account_number="12345678",
        bank="EXAMPLE BANK PLC",
        reference="Online Banking rent",
    )
    # A word the column breaks mid-way is joined again; a break at a space keeps the space.
    assert international.description == (
        "/TI27051100000001//International Inward Remittance EXAMPLE SENDER "
        "REFED-NO: PTG1S02600000001BUSIN-NO:TI27051100000001 /PURP/OTHR///URI/HOUSEHOLD GOODS"
    )
    assert international.value_date == date(2027, 5, 12)
    assert international.raw["Trans. Date"] == "11/05/27"
    assert international.raw["Value Date"] == "12/05/27"


def test_boc_payment_fields() -> None:
    local, international, transfer = _rows(
        _boc(BocStatement(pages=[[LOCAL_OUT, INTERNATIONAL_IN, TRANSFER_IN]]))
    )

    assert local.payment_kind == local.bank_type == "Local Clearing And Outward Remittance"
    assert local.bank_reference == "PTG2I02600000001"
    assert local.purpose_code is None and local.remittance_info is None

    assert international.payment_kind == "International Inward Remittance"
    assert international.bank_reference == "TI27051100000001"
    assert international.purpose_code == "OTHR"
    assert international.remittance_info == "HOUSEHOLD GOODS"
    assert international.counterparty == Counterparty(
        name="EXAMPLE SENDER", reference="PTG1S02600000001"
    )

    assert transfer.payment_kind == "Inward Transfer"
    assert transfer.bank_reference is None
    # The number between the name and the bank has no printed meaning: raw only.
    assert transfer.counterparty == Counterparty(
        name="A EXAMPLE", bank="Example Bank (UK) Limited, Example Branch"
    )
    assert "51234" in transfer.raw["Details"]


def test_boc_digits_outside_known_shapes_are_not_typed() -> None:
    charge = BocRow("20/05/27", "20/05/27", ("///Charging Monthly fee 12 2027",), paid_out="5.00")
    local = BocRow(
        "21/05/27",
        "21/05/27",
        ("/PTG2I02600000002//Local", "Clearing And Outward", "Remittance A Example 2027 rent"),
        paid_out="6.00",
    )
    transfer = BocRow(
        "22/05/27", "22/05/27", ("///Transfer A EXAMPLE 51234", "Other Bank PLC"), paid_out="7.00"
    )
    rows = _rows(_boc(BocStatement(opening="200.00", pages=[[charge, local, transfer]])))

    assert [row.counterparty for row in rows] == [None, None, None]
    assert [row.payment_kind for row in rows] == [
        "Charging",
        "Local Clearing And Outward Remittance",
        "Transfer",
    ]


def test_boc_sort_code_before_a_bank_name() -> None:
    # A 6-digit group right before a bank's legal name is that bank's sort code.
    row_with_bank = BocRow(
        "21/05/27",
        "21/05/27",
        (
            "/PTG2I02600000003//Local",
            "Clearing And Outward",
            "Remittance Alex Example",
            "401234 EXAMPLE BANK PLC",
            "Online Banking rent",
        ),
        paid_out="6.00",
    )
    row = _rows(_boc(BocStatement(opening="10.00", pages=[[row_with_bank]])))[0]

    assert row.counterparty == Counterparty(
        name="Alex Example",
        sort_code="401234",
        bank="EXAMPLE BANK PLC",
        reference="Online Banking rent",
    )


def _local_counterparty(*lines: str) -> Counterparty | None:
    row = BocRow("21/05/27", "21/05/27", ("///Local Clearing And Outward", *lines), paid_out="6.00")
    return _rows(_boc(BocStatement(opening="10.00", pages=[[row]])))[0].counterparty


def test_boc_bank_name_must_follow_the_sort_code() -> None:
    # Lower-case words between the digits and the legal name: not a bank name, no sort code.
    assert _local_counterparty("Remittance Alex 202701", "rent paid to EXAMPLE BANK PLC") == (
        Counterparty(name="Alex", reference="rent paid to EXAMPLE BANK PLC")
    )


def test_boc_account_number_after_the_bank_name() -> None:
    assert _local_counterparty("Remittance Alex 401234", "EXAMPLE BANK PLC", "12345678 rent") == (
        Counterparty(
            name="Alex",
            sort_code="401234",
            account_number="12345678",
            bank="EXAMPLE BANK PLC",
            reference="rent",
        )
    )


def test_boc_lone_six_digits_are_not_a_sort_code() -> None:
    # Only a sort code printed with an account number fills the counterparty's account.
    lone = BocRow(
        "21/05/27",
        "21/05/27",
        ("///Local Clearing And Outward", "Remittance Alex Example", "202701 rent"),
        paid_out="6.00",
    )
    row = _rows(_boc(BocStatement(opening="10.00", pages=[[lone]])))[0]

    assert row.description == ("///Local Clearing And Outward Remittance Alex Example 202701 rent")
    assert row.counterparty == Counterparty(name="Alex Example", reference="rent")


def test_boc_terms_pages_ignored() -> None:
    # The terms page after the last numbered page holds amounts and payment kinds.
    with_terms = _boc(BocStatement(pages=[[LOCAL_OUT]], terms=True))
    without_terms = _boc(BocStatement(pages=[[LOCAL_OUT]], terms=False))

    assert with_terms == without_terms


def test_boc_several_currencies_refused() -> None:
    content = boc_pdf(BocStatement(pages=[[LOCAL_OUT]], currencies=("GBP", "USD")))

    with pytest.raises(FileFormatError, match="several currencies"):
        _parse(content, "boc_uk_statement")


# Every layout --------------------------------------------------------------------------


def test_statement_account_and_summary_fields() -> None:
    hsbc = _hsbc(HsbcStatement(pages=[THREE_ROWS]))
    assert hsbc.account == StatementAccount(
        sort_code="401234",
        account_number="12345678",
        iban="GB99EXAM40123412345678",
        bic="EXAMGB2L",
        name="Mr A Example",
        addressee="Mr A Example",
        address="1 Example Street, Exampletown, EX1 2MP",
    )
    assert hsbc.currency == "GBP"
    assert hsbc.statement_reference == "7"
    assert hsbc.document_date is None

    lloyds = _lloyds(LloydsStatement(rows=LLOYDS_ROWS))
    assert lloyds.account == StatementAccount(
        sort_code="301234",
        account_number="12345678",
        account_type="EXAMPLE CURRENT",
        addressee="Mr A Example",
        address="1 Example Street, Exampletown, EX1 2MP",
    )
    assert lloyds.currency == "GBP"
    assert lloyds.document_date == date(2027, 7, 2)

    boc = _boc(BocStatement(pages=[[LOCAL_OUT]]))
    assert boc.account == StatementAccount(
        sort_code="401234",
        account_number="123400100000001",
        iban="GB99EXAM40123490123456",
        bic="EXAMGB2LXXX",
        name="MR A EXAMPLE",
        account_type="Personal Example GBP Account",
        customer_number="1234567890",
        clearing_account_number="90123456",
        branch=(
            "Example Bank (UK) Limited, Example Branch, 1 Example Street, Exampletown, EX1 1AA"
        ),
        addressee="MR A EXAMPLE",
        address="1 EXAMPLE STREET, EXAMPLETOWN, EX1 2MP",
    )
    assert boc.currency == "GBP"
    assert boc.statement_reference == "2031007"
    assert (boc.opening_balance, boc.closing_balance) == (Decimal("140.00"), Decimal("0.00"))
    assert (boc.money_in, boc.money_out) == (Decimal("0.00"), Decimal("140.00"))
    assert (boc.payments_in, boc.payments_out) == (0, 1)


# Self-checks ---------------------------------------------------------------------------


def test_layout_refuses_when_summary_differs() -> None:
    content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], summary={"Payments Out": "£25.00"}))

    with pytest.raises(
        FileFormatError,
        match=re.escape("the statement's money out is 25.00 but its rows add up to 21.55"),
    ):
        _parse(content, "hsbc_uk_statement")


@pytest.mark.parametrize("layout", ["hsbc_uk_statement", "lloyds_statement", "boc_uk_statement"])
def test_layout_refuses_when_printed_balance_differs(layout: Layout) -> None:
    if layout == "hsbc_uk_statement":
        rows = list(THREE_ROWS)
        rows[0] = HsbcRow(
            "VIS", ("EXAMPLE CAFE",), paid_out="4.20", date="08 May 27", balance="95.08"
        )
        content = hsbc_pdf(HsbcStatement(pages=[rows]))
    elif layout == "lloyds_statement":
        rows = [
            LLOYDS_ROWS[0],
            LloydsRow("21 Jun 27", ("A EXAMPLE",), "FPO", money_out="7.25", balance="76.67"),
        ]
        content = lloyds_pdf(LloydsStatement(rows=rows))
    else:
        wrong = BocRow(
            "24/05/27", "24/05/27", ("///Transfer A EXAMPLE",), paid_in="1.00", balance="2.00"
        )
        content = boc_pdf(BocStatement(pages=[[LOCAL_OUT, wrong]]))

    with pytest.raises(FileFormatError, match="the balance printed on page"):
        _parse(content, layout)


@pytest.mark.parametrize("layout", ["hsbc_uk_statement", "lloyds_statement", "boc_uk_statement"])
def test_layout_refuses_a_row_with_two_balances(layout: Layout) -> None:
    # A second word in the balance column of one row must not replace the first unchecked.
    if layout == "hsbc_uk_statement":
        extra = [(0, 525.0, 502.75, "1.00"), (0, 541.0, 502.75, "2.00")]
        content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], extra=extra))
    elif layout == "lloyds_statement":
        content = lloyds_pdf(LloydsStatement(rows=LLOYDS_ROWS, extra=[(0, 520.0, 352.0, "12.00")]))
    else:
        extra = [(1, 540.0, 193.5, "12.00")]
        content = boc_pdf(BocStatement(pages=[[LOCAL_OUT]], extra=extra))

    with pytest.raises(FileFormatError, match="prints two balances"):
        _parse(content, layout)


def test_layout_refuses_unplaced_words() -> None:
    # A stray amount right of the balance column, on a row's line.
    content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], extra=[(0, 572.0, 480.25, "12.00")]))

    with pytest.raises(FileFormatError, match="words outside any column on page 1"):
        _parse(content, "hsbc_uk_statement")


def test_layout_refuses_a_row_outside_the_period() -> None:
    rows = [HsbcRow("VIS", ("EXAMPLE CAFE",), paid_out="4.20", date="20 Apr 27", balance="95.80")]
    content = hsbc_pdf(HsbcStatement(pages=[rows]))

    with pytest.raises(FileFormatError, match="outside the statement period"):
        _parse(content, "hsbc_uk_statement")


def test_scanned_pdf_refused() -> None:
    with pytest.raises(
        FileFormatError, match="no text layer; scanned statements are not supported"
    ):
        _parse(scanned_pdf(), "hsbc_uk_statement")


def test_encrypted_pdf_refused() -> None:
    with pytest.raises(FileFormatError, match="encrypted"):
        _parse(encrypted_pdf(), "lloyds_statement")


def test_not_a_pdf_refused() -> None:
    with pytest.raises(FileFormatError, match="not a PDF"):
        _parse(b"Date,Amount\n", "boc_uk_statement")


def test_wrong_layout_refused() -> None:
    with pytest.raises(FileFormatError):
        _parse(lloyds_pdf(LloydsStatement(rows=LLOYDS_ROWS)), "hsbc_uk_statement")


def test_boc_field_printed_blank_is_absent() -> None:
    """Review finding: a label printed with no value was stored as '' (a blank IBAN then
    broke import_statements' CHECK)."""
    statement = _boc(BocStatement(pages=[[LOCAL_OUT]], iban=""))
    assert statement.account is not None and statement.account.iban is None


def test_statement_without_a_printed_currency_is_refused() -> None:
    """Third review: without one, rows took the upload's default currency, so a currency
    account's amounts could be booked as GBP."""
    content = boc_pdf(BocStatement(pages=[[LOCAL_OUT]], currencies=("gbp",)))
    with pytest.raises(FileFormatError, match="doesn't print its currency"):
        _parse(content, "boc_uk_statement")
