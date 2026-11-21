import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    ParsedStatement,
    SkippedRow,
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
    RevolutCurrentStatement,
    RevolutRow,
    RevolutSavingsRow,
    RevolutSavingsStatement,
    TemboRow,
    TemboStatement,
    boc_pdf,
    encrypted_pdf,
    hsbc_pdf,
    lloyds_pdf,
    revolut_current_pdf,
    revolut_savings_pdf,
    scanned_pdf,
    tembo_pdf,
)

LONDON = ZoneInfo("Europe/London")
type Layout = Literal[
    "hsbc_uk_statement",
    "lloyds_statement",
    "boc_uk_statement",
    "revolut_current_statement",
    "revolut_savings_statement",
    "tembo_statement",
]
LAYOUTS: list[Layout] = [
    "hsbc_uk_statement",
    "lloyds_statement",
    "boc_uk_statement",
    "tembo_statement",
]


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


def _tembo(statement: TemboStatement) -> ParsedStatement:
    return _parse(tembo_pdf(statement), "tembo_statement")


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


# Tembo ---------------------------------------------------------------------------------

TEMBO_ROWS = [
    TemboRow("03/05/2027", "Deposit", "£50.00"),
    TemboRow("10/05/2027", "Withdrawal", "£5.50"),
    TemboRow("31/05/2027", "Interest", "£0.12"),
    TemboRow("31/05/2027", "Interest Adjustment", "-£0.02"),
]


def test_tembo_rows_and_summary() -> None:
    statement = _tembo(TemboStatement(pages=[TEMBO_ROWS]))
    rows = _rows(statement)
    assert [(r.booking_date, r.bank_type, r.quantity, r.balance) for r in rows] == [
        (date(2027, 5, 3), "Deposit", Decimal("50.00"), Decimal("50.00")),
        (date(2027, 5, 10), "Withdrawal", Decimal("-5.50"), Decimal("44.50")),
        (date(2027, 5, 31), "Interest", Decimal("0.12"), Decimal("44.62")),
        (date(2027, 5, 31), "Interest Adjustment", Decimal("-0.02"), Decimal("44.60")),
    ]
    assert [r.description for r in rows] == [r.bank_type for r in rows]
    assert {r.currency for r in rows} == {"GBP"}
    assert rows[1].raw == {
        "Date": "10/05/2027",
        "Transaction type": "Withdrawal",
        "Amount": "£5.50",
        "Balance": "£44.50",
    }
    assert statement.period == (date(2027, 5, 3), date(2027, 5, 31))
    # Made on demand, it may stop partway through its last day.
    assert not statement.period_complete
    assert statement.account == StatementAccount(
        account_reference="ACC-EX01AB-CD23EF",
        account_type="Regular Saver",
        addressee="Alex Example",
        address="FLAT 1 Example House 2 Example Road, Exampletown, EX1 2MP",
    )
    assert statement.currency == "GBP"
    # The Plan value: undated, as the last day may not be listed in full.
    assert (statement.closing_balance, statement.closing_balance_date) == (Decimal("44.60"), None)
    # Tembo prints no opening balance and no totals: nothing is stored for them.
    assert (statement.opening_balance, statement.money_in, statement.money_out) == (
        None,
        None,
        None,
    )
    assert (statement.payments_in, statement.payments_out) == (None, None)
    assert (statement.available_balance, statement.available_balance_date) == (
        Decimal("44.60"),
        date(2027, 6, 1),
    )
    assert statement.document_date == date(2027, 6, 1)
    assert statement.account_opened_on == date(2027, 2, 3)


def test_tembo_available_to_withdraw_is_its_own_field() -> None:
    statement = _tembo(TemboStatement(pages=[TEMBO_ROWS], available="£40.00"))
    assert statement.available_balance == Decimal("40.00")
    assert statement.closing_balance == Decimal("44.60")


def test_tembo_multipage_and_terms_page() -> None:
    more = [
        TemboRow("12/05/2027", "Transfer In", "£1,000.00"),
        TemboRow("20/05/2027", "Transfer Out", "£200.00"),
    ]
    statement = _tembo(TemboStatement(pages=[TEMBO_ROWS[:2], more, TEMBO_ROWS[2:]]))
    rows = _rows(statement)
    assert [r.quantity for r in rows] == [
        Decimal("50.00"),
        Decimal("-5.50"),
        Decimal("1000.00"),
        Decimal("-200.00"),
        Decimal("0.12"),
        Decimal("-0.02"),
    ]
    assert [r.position for r in rows] == list(range(6))
    assert statement.closing_balance == Decimal("844.60")


def test_tembo_newest_first_is_read_oldest_first() -> None:
    oldest_first = _rows(_tembo(TemboStatement(pages=[TEMBO_ROWS[:2], TEMBO_ROWS[2:3]])))
    newest_first = _rows(
        _tembo(TemboStatement(pages=[TEMBO_ROWS[:2], TEMBO_ROWS[2:3]], newest_first=True))
    )
    assert newest_first == oldest_first


def test_tembo_rows_out_of_date_order_refused() -> None:
    rows = [TEMBO_ROWS[1], TEMBO_ROWS[0], TEMBO_ROWS[2]]
    with pytest.raises(FileFormatError, match="aren't in date order"):
        _tembo(TemboStatement(pages=[rows]))


def test_tembo_plan_value_must_equal_the_rows() -> None:
    with pytest.raises(
        FileFormatError,
        match=re.escape("its rows give 44.60, but its closing balance is 45.00"),
    ):
        _tembo(TemboStatement(pages=[TEMBO_ROWS], plan_value="£45.00"))


def test_tembo_unknown_transaction_type_refused() -> None:
    rows = [TemboRow("03/05/2027", "Bonus", "£5.00")]
    with pytest.raises(FileFormatError, match="transaction type the statement terms don't list"):
        _tembo(TemboStatement(pages=[rows]))


def test_tembo_money_in_printed_negative_refused() -> None:
    rows = [TemboRow("03/05/2027", "Deposit", "-£5.00", balance="£5.00")]
    with pytest.raises(FileFormatError, match="is a Deposit printed as money out"):
        _tembo(TemboStatement(pages=[rows], plan_value="£5.00"))


def test_tembo_withdrawal_printed_negative_is_money_out() -> None:
    rows = [TEMBO_ROWS[0], TemboRow("10/05/2027", "Withdrawal", "-£5.50")]
    assert _rows(_tembo(TemboStatement(pages=[rows])))[1].quantity == Decimal("-5.50")


def test_tembo_amount_without_pounds_refused() -> None:
    rows = [TemboRow("03/05/2027", "Deposit", "5.00", balance="£5.00")]
    with pytest.raises(FileFormatError, match="isn't in pounds"):
        _tembo(TemboStatement(pages=[rows], plan_value="£5.00"))


def test_tembo_row_without_an_amount_refused() -> None:
    rows = [TemboRow("03/05/2027", "Deposit", "", balance="£0.00")]
    with pytest.raises(FileFormatError, match="has 0 amounts, not one"):
        _tembo(TemboStatement(pages=[rows], plan_value="£0.00"))


def test_tembo_row_with_two_amounts_refused() -> None:
    extra = [(0, 381.0, 391.7, "£1.00")]
    with pytest.raises(FileFormatError, match="has 2 amounts, not one"):
        _tembo(TemboStatement(pages=[TEMBO_ROWS], extra=extra))


def test_tembo_no_printed_balance_refused() -> None:
    rows = [TemboRow("03/05/2027", "Deposit", "£5.00", balance="")]
    with pytest.raises(FileFormatError, match="no opening balance and no row balance"):
        _tembo(TemboStatement(pages=[rows], plan_value="£5.00"))


@pytest.mark.parametrize(
    ("start", "end"),
    [("01/05/2027", "31/05/2027"), ("03/05/2027", "01/06/2027"), ("04/05/2027", "31/05/2027")],
)
def test_tembo_period_must_run_from_the_first_row_to_the_last(start: str, end: str) -> None:
    """Review: with the first row unread, the opening taken from the next printed balance
    hid it, and the whole period was taken as listed."""
    with pytest.raises(FileFormatError, match="doesn't run from its first row's date"):
        _tembo(TemboStatement(pages=[TEMBO_ROWS], start=start, end=end))


def test_tembo_missing_page_refused() -> None:
    """Review: a page with +£20 and -£20 left out of the file kept every check passing."""
    pair = [
        TemboRow("12/05/2027", "Transfer In", "£20.00"),
        TemboRow("13/05/2027", "Transfer Out", "£20.00"),
    ]
    pages = [TEMBO_ROWS[:2], pair, TEMBO_ROWS[2:]]
    assert len(_rows(_tembo(TemboStatement(pages=pages)))) == 6
    with pytest.raises(FileFormatError, match="may be missing a page"):
        _tembo(TemboStatement(pages=pages, omit_page=2))
    # Its table unreadable, its page number not: no row of it can be read.
    with pytest.raises(FileFormatError, match="page 2 has no transaction table"):
        _tembo(TemboStatement(pages=pages, unreadable_page=2))
    # Review: its header readable, its rows not.
    with pytest.raises(FileFormatError, match="page 2's transaction table has no row"):
        _tembo(TemboStatement(pages=pages, unreadable_rows_page=2))
    # The last page, the terms, left out: the count no longer matches.
    with pytest.raises(FileFormatError, match="page 1 of 3 doesn't print that page number"):
        _tembo(TemboStatement(pages=pages, omit_page=4))


def test_tembo_stray_word_in_the_table_refused() -> None:
    extra = [(0, 560.0, 425.4, "12.00")]
    with pytest.raises(FileFormatError, match="words outside any column on page 1"):
        _tembo(TemboStatement(pages=[TEMBO_ROWS], extra=extra))


def test_tembo_dated_line_on_a_page_without_the_table_refused() -> None:
    # On the terms page: a row there would be read by nothing.
    extra = [(1, 62.8, 300.0, "04/05/2027"), (1, 191.1, 300.0, "Deposit")]
    with pytest.raises(FileFormatError, match="words outside any column on page 2"):
        _tembo(TemboStatement(pages=[TEMBO_ROWS], extra=extra))


def test_tembo_wrong_bank_refused() -> None:
    with pytest.raises(FileFormatError, match="not a Tembo statement"):
        _parse(lloyds_pdf(LloydsStatement(rows=LLOYDS_ROWS)), "tembo_statement")


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


@given(
    cents=st.lists(
        st.one_of(st.integers(-10000, -1), st.integers(1, 10000)), min_size=1, max_size=8
    ),
    data=st.data(),
)
def test_hsbc_reconciles_every_printed_balance(cents: list[int], data: st.DataObject) -> None:
    # Positive opening and closing use the ordinary summary layout. Intermediate
    # balances can be overdrawn, which the bank prints with a trailing D.
    def printed(amount: Decimal) -> str:
        return f"{abs(amount):,.2f}" + (" D" if amount < 0 else "")

    opening = Decimal(max(0, -sum(cents)) + 1).scaleb(-2)
    balance = opening
    balances = []
    rows = []
    for i, value in enumerate(cents):
        amount = Decimal(value).scaleb(-2)
        balance += amount
        balances.append(balance)
        rows.append(
            HsbcRow(
                "CR" if value > 0 else "VIS",
                ("EXAMPLE SHOP",),
                paid_in=str(amount) if value > 0 else None,
                paid_out=str(-amount) if value < 0 else None,
                date=f"{8 + i:02} May 27",
                balance=printed(balance),
            )
        )
    statement = HsbcStatement(pages=[rows], opening=str(opening))
    parsed = _hsbc(statement)
    assert parsed.opening_balance == opening and parsed.closing_balance == balance
    assert [row.balance for row in _rows(parsed)] == balances
    assert [row.quantity for row in _rows(parsed)] == [Decimal(n).scaleb(-2) for n in cents]

    index = data.draw(st.integers(0, len(rows) - 1), label="misprinted balance")
    altered = rows.copy()
    altered[index] = replace(rows[index], balance=printed(balances[index] + Decimal("0.01")))
    with pytest.raises(FileFormatError, match="the balance printed on page"):
        _hsbc(replace(statement, pages=[altered]))


def test_layout_refuses_when_summary_differs() -> None:
    content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], summary={"Payments Out": "£25.00"}))

    with pytest.raises(
        FileFormatError,
        match=re.escape("the statement's money out is 25.00 but its rows add up to 21.55"),
    ):
        _parse(content, "hsbc_uk_statement")


@pytest.mark.parametrize("layout", LAYOUTS)
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
    elif layout == "tembo_statement":
        wrong = TemboRow("20/05/2027", "Withdrawal", "£10.00", balance="£45.00")
        content = tembo_pdf(TemboStatement(pages=[[TEMBO_ROWS[0], wrong]]))
    else:
        wrong = BocRow(
            "24/05/27", "24/05/27", ("///Transfer A EXAMPLE",), paid_in="1.00", balance="2.00"
        )
        content = boc_pdf(BocStatement(pages=[[LOCAL_OUT, wrong]]))

    with pytest.raises(FileFormatError, match="the balance printed on page"):
        _parse(content, layout)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_layout_refuses_a_row_with_two_balances(layout: Layout) -> None:
    # A second word in the balance column of one row must not replace the first unchecked.
    if layout == "hsbc_uk_statement":
        extra = [(0, 525.0, 502.75, "1.00"), (0, 541.0, 502.75, "2.00")]
        content = hsbc_pdf(HsbcStatement(pages=[THREE_ROWS], extra=extra))
    elif layout == "lloyds_statement":
        content = lloyds_pdf(LloydsStatement(rows=LLOYDS_ROWS, extra=[(0, 520.0, 352.0, "12.00")]))
    elif layout == "tembo_statement":
        extra = [(0, 500.0, 391.7, "£1.00")]
        content = tembo_pdf(TemboStatement(pages=[TEMBO_ROWS], extra=extra))
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


def test_mailing_block_passes_over_the_other_column() -> None:
    """Fourth review: a line of the right-hand column between address lines ended the
    block, silently dropping the rest of the address."""
    from decimal import Decimal as D

    from lararium.modules.finance.importing.pdf.layout import mailing_block
    from lararium.modules.finance.importing.pdf.words import Word

    def line(top: str, x0: str, text: str) -> list[Word]:
        return [Word(text, D(x0), D(x0) + 50, D(top), 0, D(8))]

    lines = [
        line("155", "60.8", "Mr A Example"),
        line("166", "60.8", "1 Example Street"),
        line("170.5", "360", "Opening Balance"),
        line("177", "60.8", "Exampletown"),
        line("188", "60.8", "EX1 2MP"),
    ]
    assert mailing_block(lines, 0, D(300)) == (
        "Mr A Example",
        "1 Example Street, Exampletown, EX1 2MP",
    )


# Revolut --------------------------------------------------------------------------------

REVOLUT_ROWS = [
    RevolutRow(
        "Oct 2, 2027",
        "Payment from A EXAMPLE",
        paid_in="£1,250.00",
        balance="£1,350.00",
        details=("Reference: Rent share", "From: A EXAMPLE, 87654321"),
    ),
    RevolutRow(
        "Oct 3, 2027",
        "Example Cafe",
        paid_out="£4.20",
        balance="£1,345.80",
        details=("To: Example Cafe, Exampletown, GBR", "Card: 123456******1234"),
    ),
    RevolutRow(
        "Oct 4, 2027",
        "Example Software",
        paid_out="£15.40",
        balance="£1,330.40",
        details=("To: Example Software, Example City, CA", "Card: 123456******1234"),
        rate=("Revolut Rate £1.00 = $1.30 (ECB rate* £1.00 = $1.31)", "$20.00"),
    ),
    RevolutRow("Oct 5, 2027", "To GBP Example account", paid_out="£30.40", balance="£1,300.00"),
]
REVOLUT_SUMMARY = {
    "opening": "£100.00",
    "money_out": "£50.00",
    "money_in": "£1,250.00",
    "closing": "£1,300.00",
}


def _revolut(**changes: object) -> ParsedStatement:
    statement = RevolutCurrentStatement(pages=[REVOLUT_ROWS], **REVOLUT_SUMMARY)  # pyright: ignore[reportArgumentType]
    return _parse(
        revolut_current_pdf(replace(statement, **changes)),  # pyright: ignore[reportArgumentType]
        "revolut_current_statement",
    )


def test_revolut_current_rows_and_summary() -> None:
    statement = _revolut()

    rows = _rows(statement)
    assert [(row.booking_date, row.description, row.quantity, row.balance) for row in rows] == [
        (date(2027, 10, 2), "Payment from A EXAMPLE", Decimal("1250.00"), Decimal("1350.00")),
        (date(2027, 10, 3), "Example Cafe", Decimal("-4.20"), Decimal("1345.80")),
        (date(2027, 10, 4), "Example Software", Decimal("-15.40"), Decimal("1330.40")),
        (date(2027, 10, 5), "To GBP Example account", Decimal("-30.40"), Decimal("1300.00")),
    ]
    assert all(row.bank_type is None and row.currency == "GBP" for row in rows)
    assert statement.period == (date(2027, 10, 1), date(2027, 10, 31))
    assert (statement.opening_balance, statement.closing_balance) == (
        Decimal("100.00"),
        Decimal("1300.00"),
    )
    assert (statement.money_in, statement.money_out) == (Decimal("1250.00"), Decimal("50.00"))
    assert statement.currency == "GBP"
    assert statement.document_date == date(2027, 11, 2)
    assert statement.account == StatementAccount(
        sort_code="040075",
        account_number="12345678",
        iban="GB33REVO00996912345678",
        bic="REVOGB21",
        account_type="Account (Current Account)",
        addressee="A EXAMPLE",
        address="1 Example Street, Flat 2, Exampletown, EX1 2MP",
    )


def test_revolut_current_payment_details() -> None:
    transfer, card, foreign, own = _rows(_revolut())

    assert transfer.counterparty == Counterparty(
        name="A EXAMPLE", account_number="87654321", reference="Rent share"
    )
    assert transfer.card_number is None
    assert transfer.raw["From"] == "A EXAMPLE, 87654321"
    # A card payment's other side is the merchant as printed, location and all.
    assert card.counterparty == Counterparty(name="Example Cafe, Exampletown, GBR")
    assert card.card_number == "123456******1234"
    assert (
        foreign.original_currency,
        foreign.original_amount,
        foreign.fx_rate,
        foreign.fx_reference_rate,
    ) == ("USD", Decimal("-20.00"), Decimal("1.30"), Decimal("1.31"))
    assert own.counterparty is None and own.original_currency is None


def test_revolut_current_pending_and_reverted_rows_are_skipped() -> None:
    pending = RevolutRow(
        "Oct 30, 2027",
        "Example Shop",
        paid_out="£9.99",
        details=("To: Example Shop, Exampletown", "Card: 123456******1234"),
    )
    reverted = RevolutRow("Oct 6, 2027", "Example Fares", paid_out="£2.80")

    statement = _revolut(pending=[pending], reverted=[reverted])

    skipped = [row for row in statement.rows if isinstance(row, SkippedRow)]
    assert [(row.position, row.reason, row.raw["Description"]) for row in skipped] == [
        (4, "pending: not in the statement's balance", "Example Shop"),
        (5, "reverted: not in the statement's balance", "Example Fares"),
    ]
    assert len(statement.rows) == 6


def test_revolut_current_continues_over_pages() -> None:
    statement = _revolut(pages=[REVOLUT_ROWS[:2], REVOLUT_ROWS[2:]])

    assert [row.description for row in _rows(statement)][2:] == [
        "Example Software",
        "To GBP Example account",
    ]


def test_revolut_current_negative_balance() -> None:
    rows = [
        RevolutRow("Oct 2, 2027", "Example Fares", paid_out="£102.50", balance="-£2.50"),
        RevolutRow("Oct 3, 2027", "From GBP Example account", paid_in="£2.50", balance="£0.00"),
    ]
    statement = _revolut(pages=[rows], money_out="£102.50", money_in="£2.50", closing="£0.00")

    assert [row.balance for row in _rows(statement)] == [Decimal("-2.50"), Decimal("0.00")]


def test_revolut_current_refuses_money_in_two_currencies() -> None:
    with pytest.raises(FileFormatError, match=r"more than one currency \(USD, GBP\)"):
        _revolut(other=("USD", "$", ("$5.00", "$0.00", "$0.00", "$5.00")))


def test_revolut_current_refuses_a_total_unlike_its_product() -> None:
    with pytest.raises(FileFormatError, match="total differs from its product"):
        _revolut(total=("£100.00", "£50.00", "£1,250.00", "£1,301.00"))


def test_revolut_current_refuses_an_unknown_detail_line() -> None:
    with pytest.raises(FileFormatError, match="words outside any column on page 2"):
        _revolut(extra=[(1, 124.76, 589.0, "Note: unexpected")])


def test_revolut_current_refuses_a_rate_not_per_one_pound() -> None:
    rows = list(REVOLUT_ROWS)
    rows[2] = replace(
        rows[2], rate=("Revolut Rate £2.00 = $2.60 (ECB rate* £1.00 = $1.31)", "$20.00")
    )

    with pytest.raises(FileFormatError, match="rate is not quoted per one GBP"):
        _revolut(pages=[rows])


def test_revolut_current_refuses_rates_in_two_currencies() -> None:
    rows = list(REVOLUT_ROWS)
    rows[2] = replace(
        rows[2], rate=("Revolut Rate £1.00 = $1.30 (ECB rate* £1.00 = $1.31)", "€20.00")
    )

    with pytest.raises(FileFormatError, match="disagree on the currency"):
        _revolut(pages=[rows])


def test_revolut_current_refuses_an_unknown_currency_symbol() -> None:
    rows = list(REVOLUT_ROWS)
    rows[2] = replace(
        rows[2], rate=("Revolut Rate £1.00 = ¤1.30 (ECB rate* £1.00 = ¤1.31)", "¤20.00")
    )

    with pytest.raises(FileFormatError, match="in a currency this layout doesn't know"):
        _revolut(pages=[rows])


SAVINGS_ROWS = [
    RevolutSavingsRow("Oct 1, 2027", "Deposit", paid_in="£1,000.00", balance="£1,500.00"),
    RevolutSavingsRow("Oct 1, 2027", "Gross Interest", paid_in="£0.12", balance="£1,500.12"),
    RevolutSavingsRow("Oct 2, 2027", "Withdrawal", paid_out="£200.00", balance="£1,300.12"),
    RevolutSavingsRow("Oct 2, 2027", "Gross Interest", paid_in="£0.10", balance="£1,300.22"),
]
SAVINGS_TOTALS = {
    "Opening Balance": "£500.00",
    "Total Deposit": "£1,000.00",
    "Total Withdrawal": "£200.00",
    "Total Interest Paid": "£0.22",
    "Closing Balance": "£1,300.22",
}


def _savings(pages: list[list[RevolutSavingsRow]] | None = None, **totals: str) -> ParsedStatement:
    statement = RevolutSavingsStatement(
        pages=pages or [SAVINGS_ROWS], totals=SAVINGS_TOTALS | totals
    )
    return _parse(revolut_savings_pdf(statement), "revolut_savings_statement")


def test_revolut_savings_rows_and_summary() -> None:
    statement = _savings()

    rows = _rows(statement)
    assert [(row.booking_date, row.bank_type, row.quantity, row.balance) for row in rows] == [
        (date(2027, 10, 1), "Deposit", Decimal("1000.00"), Decimal("1500.00")),
        (date(2027, 10, 1), "Gross Interest", Decimal("0.12"), Decimal("1500.12")),
        (date(2027, 10, 2), "Withdrawal", Decimal("-200.00"), Decimal("1300.12")),
        (date(2027, 10, 2), "Gross Interest", Decimal("0.10"), Decimal("1300.22")),
    ]
    assert rows[0].description == "Deposit"
    assert statement.period == (date(2027, 10, 1), date(2027, 10, 31))
    assert (statement.money_in, statement.money_out) == (Decimal("1000.22"), Decimal("200.00"))
    assert (statement.opening_balance, statement.closing_balance) == (
        Decimal("500.00"),
        Decimal("1300.22"),
    )
    assert statement.currency == "GBP"
    assert statement.document_date == date(2027, 11, 2)
    assert statement.account == StatementAccount(
        sort_code="040075",
        account_number="12345678",
        account_type="Example account - Instant Access - Example Bank",
        addressee="A EXAMPLE",
        address="1 Example Street, Exampletown, EX1 2MP, United Kingdom",
    )


def test_revolut_savings_continues_over_pages() -> None:
    statement = _savings([SAVINGS_ROWS[:3], SAVINGS_ROWS[3:]])

    assert len(_rows(statement)) == 4


def test_revolut_savings_checks_each_kind_total() -> None:
    # Money in still adds up; the interest and deposit totals don't.
    with pytest.raises(
        FileFormatError,
        match=re.escape(
            "the statement's Total Deposit is 1000.10 but its Deposit rows add up to 1000.00"
        ),
    ):
        _savings(**{"Total Deposit": "£1,000.10", "Total Interest Paid": "£0.12"})


def test_revolut_savings_refuses_an_unknown_kind() -> None:
    rows = [
        *SAVINGS_ROWS,
        RevolutSavingsRow("Oct 3, 2027", "Fee", paid_out="£1.00", balance="£1,299.22"),
    ]

    with pytest.raises(FileFormatError, match="'Fee', which the summary doesn't total"):
        _savings([rows])


def test_revolut_savings_refuses_a_kind_in_the_wrong_column() -> None:
    rows = list(SAVINGS_ROWS)
    rows[1] = RevolutSavingsRow(
        "Oct 1, 2027", "Gross Interest", paid_out="£0.12", balance="£1,500.12"
    )

    with pytest.raises(FileFormatError, match="Gross Interest is not in the Money in column"):
        _savings([rows])


@pytest.mark.parametrize("layout", ["revolut_current_statement", "revolut_savings_statement"])
def test_revolut_refuses_when_printed_balance_differs(layout: Layout) -> None:
    if layout == "revolut_current_statement":
        rows = list(REVOLUT_ROWS)
        rows[1] = replace(rows[1], balance="£1,345.08")
        content = revolut_current_pdf(
            RevolutCurrentStatement(pages=[rows], **REVOLUT_SUMMARY)  # pyright: ignore[reportArgumentType]
        )
    else:
        rows = list(SAVINGS_ROWS)
        rows[1] = replace(rows[1], balance="£1,500.21")
        content = revolut_savings_pdf(RevolutSavingsStatement(pages=[rows], totals=SAVINGS_TOTALS))

    with pytest.raises(FileFormatError, match="the balance printed on page"):
        _parse(content, layout)


def test_revolut_wrong_layout_refused() -> None:
    savings = revolut_savings_pdf(
        RevolutSavingsStatement(pages=[SAVINGS_ROWS], totals=SAVINGS_TOTALS)
    )
    with pytest.raises(FileFormatError, match="not a Revolut current account statement"):
        _parse(savings, "revolut_current_statement")
    with pytest.raises(FileFormatError, match="not a Revolut savings statement"):
        _parse(
            revolut_current_pdf(RevolutCurrentStatement(pages=[REVOLUT_ROWS])),
            "revolut_savings_statement",
        )


def test_revolut_statement_made_on_its_last_day_has_an_undated_closing() -> None:
    # Made partway through 31 Oct: later payments that day would move the closing.
    statement = _revolut(generated="Oct 31, 2027")

    assert statement.closing_balance == Decimal("1300.00")
    assert statement.closing_balance_date is None
    assert _revolut().closing_balance_date == date(2027, 10, 31)


def test_skipped_rows_count_towards_the_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from lararium.modules.finance.importing import parsed

    monkeypatch.setattr(parsed, "MAX_IMPORT_ROWS", len(REVOLUT_ROWS))
    pending = RevolutRow("Oct 30, 2027", "Example Shop", paid_out="£9.99")

    with pytest.raises(FileFormatError, match="more than 4 rows"):
        _revolut(pending=[pending])


def test_revolut_current_whole_amounts_in_a_currency_without_pence() -> None:
    rows = list(REVOLUT_ROWS)
    rows[2] = replace(
        rows[2], rate=("Revolut Rate £1.00 = ¥190.50 (ECB rate* £1.00 = ¥191)", "¥3,000")
    )

    foreign = _rows(_revolut(pages=[rows]))[2]

    assert (foreign.original_currency, foreign.original_amount) == ("JPY", Decimal("-3000"))
    assert (foreign.fx_rate, foreign.fx_reference_rate) == (Decimal("190.50"), Decimal("191"))


def test_revolut_current_refuses_a_whole_amount_in_a_currency_with_cents() -> None:
    rows = list(REVOLUT_ROWS)
    rows[2] = replace(rows[2], rate=("Revolut Rate £1.00 = $1.30 (ECB rate* £1.00 = $1.31)", "$20"))

    with pytest.raises(FileFormatError, match="original amount is not an amount"):
        _revolut(pages=[rows])
