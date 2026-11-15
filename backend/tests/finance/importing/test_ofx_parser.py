import time
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from lararium.modules.finance.errors import FileFormatError
from lararium.modules.finance.importing import parsed
from lararium.modules.finance.importing.ofx_parser import PARSER_VERSION, parse_ofx
from lararium.modules.finance.importing.parsed import (
    Counterparty,
    ParsedRow,
    ParsedStatement,
    RowError,
    StatementAccount,
)
from lararium.modules.finance.importing.profiles import OfxProfileConfig

SGML_HEADER = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

"""

XML_HEADER = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
"""

SGML_TRANSACTIONS = """<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20261110
<TRNAMT>-3.50
<FITID>EXB0001
<NAME>Example Cafe
<MEMO>Card 1234
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20261111
<TRNAMT>2000.00
<FITID>EXB0002
<NAME>Example Employer
</STMTTRN>"""


def _sgml_statement(transactions: str, *, extra: str = "") -> str:
    return f"""<STMTRS>
<CURDEF>GBP
<BANKACCTFROM>
<BANKID>401234
<ACCTID>12345678
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20261101
<DTEND>20261130
{transactions}
</BANKTRANLIST>
{extra}
</STMTRS>"""


def _sgml(*statements: str) -> bytes:
    wrapped = "\n".join(
        f"<STMTTRNRS>\n<TRNUID>1\n<STATUS>\n<CODE>0\n<SEVERITY>INFO\n</STATUS>\n{s}\n</STMTTRNRS>"
        for s in statements
    )
    body = f"""<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>20261130120000
<LANGUAGE>ENG
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
{wrapped}
</BANKMSGSRSV1>
</OFX>
"""
    return (SGML_HEADER + body).encode("ascii")


def _transaction(**leaves: str) -> str:
    return "<STMTTRN>\n" + "".join(f"<{k}>{v}\n" for k, v in leaves.items()) + "</STMTTRN>"


XML_V2 = (
    XML_HEADER
    + """<OFX>
  <SIGNONMSGSRSV1>
    <SONRS>
      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <DTSERVER>20261130120000</DTSERVER>
      <LANGUAGE>ENG</LANGUAGE>
    </SONRS>
  </SIGNONMSGSRSV1>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <TRNUID>1</TRNUID>
      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <STMTRS>
        <CURDEF>GBP</CURDEF>
        <BANKACCTFROM>
          <BANKID>401234</BANKID>
          <ACCTID>12345678</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20261101</DTSTART>
          <DTEND>20261130</DTEND>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20261110</DTPOSTED>
            <TRNAMT>-3.50</TRNAMT>
            <FITID>EXB0001</FITID>
            <NAME>Example Cafe</NAME>
            <MEMO>Card 1234</MEMO>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20261111</DTPOSTED>
            <TRNAMT>2000.00</TRNAMT>
            <FITID>EXB0002</FITID>
            <NAME>Example Employer</NAME>
            <MEMO></MEMO>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""
).encode()


def _config(**overrides: Any) -> OfxProfileConfig:
    return OfxProfileConfig.model_validate({"format": "ofx"} | overrides)


def _parse(content: bytes, **overrides: Any) -> ParsedStatement:
    return parse_ofx(content, _config(**overrides))


def _rows(statement: ParsedStatement) -> list[ParsedRow]:
    rows = [row for row in statement.rows if isinstance(row, ParsedRow)]
    assert len(rows) == len(statement.rows), statement.rows
    return rows


def _assert_two_rows(statement: ParsedStatement) -> None:
    rows = _rows(statement)
    assert [row.position for row in rows] == [0, 1]
    assert [row.quantity for row in rows] == [Decimal("-3.50"), Decimal("2000.00")]
    assert [row.external_id for row in rows] == ["EXB0001", "EXB0002"]
    assert [row.description for row in rows] == ["Example Cafe Card 1234", "Example Employer"]
    assert [row.booking_date for row in rows] == [date(2026, 11, 10), date(2026, 11, 11)]
    assert [row.bank_type for row in rows] == ["DEBIT", "CREDIT"]
    assert all(row.currency == "GBP" for row in rows)
    assert all(row.occurred_at is None for row in rows)
    assert all(row.fee == 0 and row.balance is None for row in rows)
    assert statement.currency == "GBP"
    assert statement.closing_balance is None and statement.closing_balance_date is None


def test_parser_version() -> None:
    assert PARSER_VERSION == "ofx-1"


def test_sgml_v1_statement() -> None:
    statement = _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS)))
    _assert_two_rows(statement)
    assert _rows(statement)[0].raw == {
        "TRNTYPE": "DEBIT",
        "DTPOSTED": "20261110",
        "TRNAMT": "-3.50",
        "FITID": "EXB0001",
        "NAME": "Example Cafe",
        "MEMO": "Card 1234",
    }


def test_xml_v2_statement() -> None:
    statement = _parse(XML_V2)
    _assert_two_rows(statement)
    second = _rows(statement)[1]
    assert second.memo is None
    assert second.raw["MEMO"] == ""


def test_outflow_positive_negates() -> None:
    rows = _rows(_parse(_sgml(_sgml_statement(SGML_TRANSACTIONS)), sign="outflow_positive"))
    assert [row.quantity for row in rows] == [Decimal("3.50"), Decimal("-2000.00")]


def test_dtposted_with_offset_keeps_written_date() -> None:
    transaction = _transaction(DTPOSTED="20261110233000[-5:EST]", TRNAMT="-1.00", FITID="A")
    row = _rows(_parse(_sgml(_sgml_statement(transaction))))[0]
    assert row.booking_date == date(2026, 11, 10)
    assert row.occurred_at == datetime(2026, 11, 11, 4, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("20261110233000", datetime(2026, 11, 10, 23, 30, tzinfo=UTC)),
        ("20261110233000.250", datetime(2026, 11, 10, 23, 30, 0, 250000, tzinfo=UTC)),
        ("20261110233000[+5.30:IST]", datetime(2026, 11, 10, 18, 0, tzinfo=UTC)),
        ("20261110233000[0]", datetime(2026, 11, 10, 23, 30, tzinfo=UTC)),
        ("20261110233000[:GMT]", datetime(2026, 11, 10, 23, 30, tzinfo=UTC)),
    ],
)
def test_dtposted_time_forms(written: str, expected: datetime) -> None:
    transaction = _transaction(DTPOSTED=written, TRNAMT="-1.00")
    row = _rows(_parse(_sgml(_sgml_statement(transaction))))[0]
    assert row.booking_date == date(2026, 11, 10)
    assert row.occurred_at == expected
    assert row.occurred_at is not None and row.occurred_at.tzinfo is not None


@pytest.mark.parametrize("written", ["2026111", "20261310", "20261110256000", "20261110x", ""])
def test_malformed_dtposted_is_row_error(written: str) -> None:
    transaction = _transaction(DTPOSTED=written, TRNAMT="-1.00")
    good = _transaction(DTPOSTED="20261111", TRNAMT="2.00")
    statement = _parse(_sgml(_sgml_statement(transaction + "\n" + good)))
    error, row = statement.rows
    assert isinstance(error, RowError)
    assert error.position == 0
    assert "DTPOSTED" in error.message
    assert isinstance(row, ParsedRow) and row.position == 1


def test_entities_unescaped() -> None:
    transaction = _transaction(DTPOSTED="20261110", TRNAMT="-5.00", NAME="M&amp;S")
    row = _rows(_parse(_sgml(_sgml_statement(transaction))))[0]
    assert row.description == "M&S"
    assert row.payee == "M&S"


def test_credit_card_statement() -> None:
    transactions = "\n".join(
        [
            _transaction(TRNTYPE="DEBIT", DTPOSTED="20261105", TRNAMT="-25.00", FITID="C1"),
            _transaction(TRNTYPE="CREDIT", DTPOSTED="20261120", TRNAMT="100.00", FITID="C2"),
        ]
    )
    card = f"""<CCSTMTRS>
<CURDEF>GBP
<CCACCTFROM>
<ACCTID>99990000
</CCACCTFROM>
<BANKTRANLIST>
{transactions}
</BANKTRANLIST>
</CCSTMTRS>"""
    content = _sgml(card).replace(b"BANKMSGSRSV1", b"CREDITCARDMSGSRSV1")
    content = content.replace(b"STMTTRNRS", b"CCSTMTTRNRS")
    statement = _parse(content)
    rows = _rows(statement)
    assert [row.quantity for row in rows] == [Decimal("-25.00"), Decimal("100.00")]
    assert statement.account == StatementAccount(account_number="99990000")


def test_ledgerbal_is_closing_balance() -> None:
    ledger = "<LEDGERBAL>\n<BALAMT>1234.56\n<DTASOF>20261130\n</LEDGERBAL>"
    statement = _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS, extra=ledger)))
    assert statement.closing_balance == Decimal("1234.56")
    assert statement.closing_balance_date == date(2026, 11, 30)
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS, extra=ledger))
    flipped = _parse(content, sign="outflow_positive")
    assert flipped.closing_balance == Decimal("-1234.56")
    without = _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS)))
    assert without.closing_balance is None and without.closing_balance_date is None


def test_ledgerbal_with_time_keeps_written_date() -> None:
    ledger = "<LEDGERBAL>\n<BALAMT>-10.00\n<DTASOF>20261130235959[-8:PST]\n</LEDGERBAL>"
    statement = _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS, extra=ledger)))
    assert statement.closing_balance == Decimal("-10.00")
    assert statement.closing_balance_date == date(2026, 11, 30)


@pytest.mark.parametrize(
    "ledger",
    [
        "<LEDGERBAL>\n<BALAMT>abc\n<DTASOF>20261130\n</LEDGERBAL>",
        "<LEDGERBAL>\n<BALAMT>1.00\n<DTASOF>2026-11-30\n</LEDGERBAL>",
        "<LEDGERBAL>\n<DTASOF>20261130\n</LEDGERBAL>",
    ],
)
def test_bad_ledgerbal_is_file_error(ledger: str) -> None:
    with pytest.raises(FileFormatError, match="LEDGERBAL"):
        _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS, extra=ledger)))


def test_multiple_statements_rejected() -> None:
    statement = _sgml_statement(SGML_TRANSACTIONS)
    with pytest.raises(FileFormatError, match="export one account per file"):
        _parse(_sgml(statement, statement))


def test_no_statement_rejected() -> None:
    content = (SGML_HEADER + "<OFX>\n<SIGNONMSGSRSV1>\n</SIGNONMSGSRSV1>\n</OFX>\n").encode()
    with pytest.raises(FileFormatError, match="export one account per file"):
        _parse(content)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"Date,Description,Amount\n10/11/2026,Coffee,-3.50\n",
        (SGML_HEADER + "<OFX>\n<BANKMSGSRSV1>\n<STMTTRNRS>\n").encode(),  # truncated
        (SGML_HEADER + "<OFX>\n</STMTRS>\n</OFX>\n").encode(),  # stray close
    ],
)
def test_not_ofx_is_file_error(content: bytes) -> None:
    with pytest.raises(FileFormatError):
        _parse(content)


def test_statement_without_transactions_is_file_error() -> None:
    with pytest.raises(FileFormatError, match="no rows"):
        _parse(_sgml(_sgml_statement("")))


def test_too_many_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsed, "MAX_IMPORT_ROWS", 1)
    with pytest.raises(FileFormatError, match="more than 1 rows"):
        _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS)))


def test_bad_trnamt_is_row_error() -> None:
    transactions = "\n".join(
        [
            _transaction(DTPOSTED="20261110", TRNAMT="12,50", FITID="B1"),
            _transaction(DTPOSTED="20261110", FITID="B2"),
            _transaction(TRNAMT="1.00", FITID="B3"),
            _transaction(DTPOSTED="20261111", TRNAMT="1.00", FITID="B4"),
        ]
    )
    statement = _parse(_sgml(_sgml_statement(transactions)))
    errors = [row for row in statement.rows if isinstance(row, RowError)]
    assert [error.position for error in errors] == [0, 1, 2]
    assert "TRNAMT" in errors[0].message
    assert "TRNAMT" in errors[1].message
    assert "DTPOSTED" in errors[2].message
    assert errors[0].raw == {"DTPOSTED": "20261110", "TRNAMT": "12,50", "FITID": "B1"}
    assert isinstance(statement.rows[3], ParsedRow)


def test_currency_overrides() -> None:
    transactions = "\n".join(
        [
            _transaction(DTPOSTED="20261110", TRNAMT="-1.00"),
            "<STMTTRN>\n<DTPOSTED>20261110\n<TRNAMT>-2.00\n"
            "<CURRENCY>\n<CURRATE>1.15\n<CURSYM>EUR\n</CURRENCY>\n</STMTTRN>",
            "<STMTTRN>\n<DTPOSTED>20261110\n<TRNAMT>-3.00\n"
            "<ORIGCURRENCY>\n<CURRATE>0.79\n<CURSYM>usd\n</ORIGCURRENCY>\n</STMTTRN>",
            "<STMTTRN>\n<DTPOSTED>20261110\n<TRNAMT>-4.00\n"
            "<CURRENCY>\n<CURSYM>EURO\n</CURRENCY>\n</STMTTRN>",
        ]
    )
    statement = _parse(_sgml(_sgml_statement(transactions)))
    first, second, third, fourth = statement.rows
    assert isinstance(first, ParsedRow) and first.currency == "GBP"
    assert isinstance(second, ParsedRow) and second.currency == "EUR"
    assert second.raw["CURRENCY/CURRATE"] == "1.15"
    assert second.raw["CURRENCY/CURSYM"] == "EUR"
    # ORIGCURRENCY records the original currency; TRNAMT is already in CURDEF.
    assert isinstance(third, ParsedRow) and third.currency == "GBP"
    assert third.raw["ORIGCURRENCY/CURSYM"] == "usd"
    assert third.raw["ORIGCURRENCY/CURRATE"] == "0.79"
    assert isinstance(fourth, RowError) and "currency" in fourth.message


def test_typed_fields() -> None:
    transaction = _transaction(
        TRNTYPE="CHECK",
        DTPOSTED="20261112",
        DTUSER="20261109",
        TRNAMT="-42.00",
        FITID="EXB0042",
        CHECKNUM="000123",
        NAME="Example Plumber",
        MEMO="Invoice 77",
    )
    statement = _parse(_sgml(_sgml_statement(transaction)))
    row = _rows(statement)[0]
    assert row.payee == "Example Plumber"
    assert row.memo == "Invoice 77"
    assert row.check_number == "000123"
    assert row.value_date == date(2026, 11, 9)
    assert row.bank_type == "CHECK"
    assert row.description == "Example Plumber Invoice 77"
    assert row.counterparty is None
    assert statement.account == StatementAccount(
        sort_code="401234", account_number="12345678", account_type="CHECKING"
    )
    assert statement.currency == "GBP"


def test_missing_optional_fields_are_none() -> None:
    row = _rows(_parse(_sgml(_sgml_statement(_transaction(DTPOSTED="20261112", TRNAMT="1")))))[0]
    assert (row.payee, row.memo, row.check_number, row.value_date) == (None, None, None, None)
    assert (row.bank_type, row.external_id, row.description) == (None, None, "")


def test_malformed_dtuser_is_row_error() -> None:
    transaction = _transaction(DTPOSTED="20261112", DTUSER="2026-11-09", TRNAMT="-1.00")
    (error,) = _parse(_sgml(_sgml_statement(transaction))).rows
    assert isinstance(error, RowError) and "DTUSER" in error.message


def test_non_uk_bank_id_is_not_a_sort_code() -> None:
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS)).replace(b"401234", b"121000248")
    account = _parse(content).account
    assert account == StatementAccount(account_number="12345678", account_type="CHECKING")


def test_utf8_header_decodes_utf8() -> None:
    transaction = _transaction(DTPOSTED="20261110", TRNAMT="-5.00", NAME="Café £")
    content = _sgml(_sgml_statement("TRANSACTION")).decode().replace("TRANSACTION", transaction)
    content = content.replace("ENCODING:USASCII", "ENCODING:UTF-8")
    row = _rows(_parse(content.encode("utf-8")))[0]
    assert row.payee == "Café £"


def test_xml_declaration_utf8_decodes_utf8() -> None:
    content = XML_V2.replace(b"Example Cafe", "Café £".encode())
    assert _rows(_parse(content))[0].payee == "Café £"


def test_default_decodes_cp1252() -> None:
    transaction = _transaction(DTPOSTED="20261110", TRNAMT="-5.00", NAME="Café £")
    text = _sgml(_sgml_statement("TRANSACTION")).decode().replace("TRANSACTION", transaction)
    row = _rows(_parse(text.encode("cp1252")))[0]
    assert row.payee == "Café £"


def test_undecodable_bytes_are_file_error() -> None:
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS)).replace(b"Example Cafe", b"Ex\x81")
    with pytest.raises(FileFormatError, match="cp1252"):
        _parse(content)


def test_empty_sgml_leaf_does_not_swallow_its_siblings() -> None:
    transaction = "<STMTTRN>\n<DTPOSTED>20261110\n<MEMO>\n<TRNAMT>-1.00\n<NAME>Shop\n</STMTTRN>"
    row = _rows(_parse(_sgml(_sgml_statement(transaction))))[0]
    assert row.quantity == Decimal("-1.00")
    assert (row.payee, row.memo) == ("Shop", None)
    assert row.raw == {"DTPOSTED": "20261110", "MEMO": "", "TRNAMT": "-1.00", "NAME": "Shop"}


def test_many_repeated_leaves_are_linear() -> None:
    # A leaf the parser doesn't read may repeat; it is kept, numbered, in raw.
    repeats = "".join("<EXTDNAME>x\n" for _ in range(50_000))
    transaction = f"<STMTTRN>\n<DTPOSTED>20261110\n<TRNAMT>-1.00\n{repeats}</STMTTRN>"
    content = _sgml(_sgml_statement(transaction))
    started = time.perf_counter()
    row = _rows(_parse(content))[0]
    assert time.perf_counter() - started < 2
    assert row.raw["EXTDNAME"] == "x" and row.raw["EXTDNAME#50000"] == "x"
    assert len(row.raw) == 50_002


@pytest.mark.parametrize("leaf", ["TRNAMT", "FITID", "DTPOSTED", "NAME"])
def test_repeated_read_leaf_is_row_error(leaf: str) -> None:
    """Third review: a STMTTRN with two transactions' leaves kept only the first, and the
    second's amount silently vanished."""
    values = {"DTPOSTED": "20261110", "TRNAMT": "-5.00", "FITID": "A1", "NAME": "Shop"}
    repeated = f"<{leaf}>{values[leaf]}\n"
    transaction = _transaction(**values).replace("</STMTTRN>", repeated + "</STMTTRN>")
    (error,) = _parse(_sgml(_sgml_statement(transaction))).rows
    assert isinstance(error, RowError) and f"gives {leaf} more than once" in error.message


def test_repeated_statement_leaf_is_file_error() -> None:
    extra = "<LEDGERBAL>\n<BALAMT>10.00\n<BALAMT>12.00\n<DTASOF>20261130\n</LEDGERBAL>"
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS, extra=extra))
    with pytest.raises(FileFormatError, match="LEDGERBAL: the record gives BALAMT more than once"):
        _parse(content)


def test_deep_nesting_is_file_error() -> None:
    content = (SGML_HEADER + "<OFX>\n" + "<A>\n" * 10_000).encode()
    with pytest.raises(FileFormatError, match="the file is nested too deeply"):
        _parse(content)


def test_period_from_dtstart_dtend() -> None:
    assert _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS))).period == (
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    assert _parse(XML_V2).period == (date(2026, 11, 1), date(2026, 11, 30))
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS)).replace(
        b"<DTEND>20261130", b"<DTEND>20261130235959[-5:EST]"
    )
    assert _parse(content).period == (date(2026, 11, 1), date(2026, 11, 30))


@pytest.mark.parametrize("leaf", [b"<DTSTART>20261101\n", b"<DTEND>20261130\n"])
def test_period_needs_both_dates(leaf: bytes) -> None:
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS)).replace(leaf, b"")
    assert _parse(content).period is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"<DTSTART>20261101", b"<DTSTART>2026-11-01"),
        (b"<DTEND>20261130", b"<DTEND>20261131"),
        (b"<DTSTART>20261101", b"<DTSTART>20261201"),  # after DTEND
    ],
)
def test_bad_period_is_file_error(old: bytes, new: bytes) -> None:
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS)).replace(old, new)
    with pytest.raises(FileFormatError, match="BANKTRANLIST"):
        _parse(content)


def test_availbal_is_available_balance() -> None:
    balances = (
        "<LEDGERBAL>\n<BALAMT>1234.56\n<DTASOF>20261130\n</LEDGERBAL>\n"
        "<AVAILBAL>\n<BALAMT>1500.00\n<DTASOF>20261129120000[-5:EST]\n</AVAILBAL>"
    )
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS, extra=balances))
    statement = _parse(content)
    assert statement.available_balance == Decimal("1500.00")
    assert statement.available_balance_date == date(2026, 11, 29)
    assert statement.closing_balance == Decimal("1234.56")
    assert _parse(content, sign="outflow_positive").available_balance == Decimal("-1500.00")
    without = _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS)))
    assert without.available_balance is None and without.available_balance_date is None


@pytest.mark.parametrize(
    "balance",
    [
        "<AVAILBAL>\n<BALAMT>x\n<DTASOF>20261130\n</AVAILBAL>",
        "<AVAILBAL>\n<BALAMT>1.00\n</AVAILBAL>",
    ],
)
def test_bad_availbal_is_file_error(balance: str) -> None:
    with pytest.raises(FileFormatError, match="AVAILBAL"):
        _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS, extra=balance)))


def test_unicode_header_decodes_utf8() -> None:
    transaction = _transaction(DTPOSTED="20261110", TRNAMT="-5.00", NAME="Café £")
    content = _sgml(_sgml_statement("TRANSACTION")).decode().replace("TRANSACTION", transaction)
    content = content.replace("ENCODING:USASCII", "ENCODING:UNICODE")
    assert _rows(_parse(content.encode("utf-8")))[0].payee == "Café £"


def test_acctid_with_sort_code_prefix_gives_account_number() -> None:
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS))
    prefixed = content.replace(b"<ACCTID>12345678", b"<ACCTID>40123412345678")
    assert _parse(prefixed).account == StatementAccount(
        sort_code="401234", account_number="12345678", account_type="CHECKING"
    )
    other = content.replace(b"<ACCTID>12345678", b"<ACCTID>99999912345678")
    account = _parse(other).account
    assert account is not None and account.account_number == "99999912345678"


def test_large_amount_negated_exactly() -> None:
    big = "1234567890123456789012345678.91"
    statement = _sgml_statement(
        _transaction(TRNTYPE="DEBIT", DTPOSTED="20261110", TRNAMT=big, FITID="X1")
    )
    rows = _rows(_parse(_sgml(statement), sign="outflow_positive"))
    assert [row.quantity for row in rows] == [Decimal(f"-{big}")]


@pytest.mark.parametrize(
    "fields", [{"CORRECTFITID": "EXB0001", "CORRECTACTION": "DELETE"}, {"CORRECTACTION": "REPLACE"}]
)
def test_correction_record_is_row_error(fields: dict[str, str]) -> None:
    """Review finding: a correction (it deletes or replaces another transaction) was booked
    as a transaction of its own, counting the money twice."""
    original = _transaction(DTPOSTED="20261110", TRNAMT="-50.00", FITID="EXB0001")
    correction = _transaction(DTPOSTED="20261111", TRNAMT="-50.00", FITID="EXB0002", **fields)
    row, error = _parse(_sgml(_sgml_statement(original + "\n" + correction))).rows
    assert isinstance(row, ParsedRow)
    assert isinstance(error, RowError) and "corrects transaction" in error.message


@pytest.mark.parametrize("aggregate", ["STMTTRN", "LEDGERBAL"])
def test_unclosed_aggregate_is_file_error(aggregate: str) -> None:
    """Review finding: an aggregate left open was read as an empty leaf, so its contents
    moved into its parent and a transaction or the balance silently vanished."""
    first = _transaction(DTPOSTED="20261102", TRNAMT="-500.00", FITID="A")
    second = _transaction(DTPOSTED="20261103", TRNAMT="-5.00", FITID="B")
    extra = "<LEDGERBAL>\n<BALAMT>10.00\n<DTASOF>20261130\n</LEDGERBAL>"
    if aggregate == "STMTTRN":
        content = _sgml_statement(first.removesuffix("</STMTTRN>") + "\n" + second, extra=extra)
    else:
        content = _sgml_statement(first + "\n" + second, extra=extra.replace("</LEDGERBAL>", ""))
    with pytest.raises(FileFormatError, match=f"<{aggregate}> is never closed"):
        _parse(_sgml(content))


def test_payee_aggregate_account_paid_to_and_rate_are_typed() -> None:
    """Third review: PAYEE, BANKACCTTO and CURRATE were only in raw (Decision 18)."""
    transaction = (
        "<STMTTRN>\n<TRNTYPE>DEBIT\n<DTPOSTED>20261110\n<TRNAMT>-12.00\n<FITID>P1\n"
        "<PAYEE>\n<NAME>Example Landlord\n<ADDR1>1 Example Road\n</PAYEE>\n"
        "<BANKACCTTO>\n<BANKID>201234\n<ACCTID>87654321\n<ACCTTYPE>CHECKING\n</BANKACCTTO>\n"
        "<ORIGCURRENCY>\n<CURRATE>1.1650\n<CURSYM>EUR\n</ORIGCURRENCY>\n</STMTTRN>"
    )
    (row,) = _rows(_parse(_sgml(_sgml_statement(transaction))))
    assert (row.payee, row.description) == ("Example Landlord", "Example Landlord")
    assert row.counterparty == Counterparty(sort_code="201234", account_number="87654321")
    assert (row.original_currency, row.exchange_rate) == ("EUR", Decimal("1.1650"))
    assert row.currency == "GBP"


def test_cdata_value_is_read() -> None:
    """Third review: the tokenizer dropped CDATA, so a memo came back empty, even in raw."""
    transaction = _transaction(DTPOSTED="20261110", TRNAMT="-1.00", FITID="Q1")
    transaction = transaction.replace(
        "</STMTTRN>", "<MEMO><![CDATA[Rent & <bills>]]></MEMO>\n</STMTTRN>"
    )
    (row,) = _rows(_parse(_sgml(_sgml_statement(transaction))))
    assert row.memo == "Rent & <bills>" and row.raw["MEMO"] == "Rent & <bills>"


def test_generation_day_is_the_document_date() -> None:
    # SONRS's DTSERVER: the day the bank made the file.
    assert _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS))).document_date == date(2026, 11, 30)


@pytest.mark.parametrize("where", ["SONRS", "STMTTRNRS"])
def test_failed_response_status_is_file_error(where: str) -> None:
    """A STATUS other than CODE 0 is the bank saying the response failed or is incomplete."""
    content = _sgml(_sgml_statement(SGML_TRANSACTIONS)).decode()
    position = content.index("<CODE>0", content.index(f"<{where}>"))
    content = content[:position] + "<CODE>2000\n<SEVERITY>ERROR" + content[position + 7 :]
    content = content.replace("<SEVERITY>ERROR\n<SEVERITY>INFO", "<SEVERITY>ERROR", 1)
    with pytest.raises(FileFormatError, match="status 2000 \\(ERROR\\)"):
        _parse(content.encode())


def test_bare_ampersand_is_kept_as_written() -> None:
    """Fourth review: a bare '&' before a legacy entity name was rewritten ('B&LTD' -> 'B<D')."""
    transaction = _transaction(
        DTPOSTED="20261110", TRNAMT="-1.00", FITID="E1", NAME="B&LTD SHOP", MEMO="A &amp; B"
    )
    (row,) = _rows(_parse(_sgml(_sgml_statement(transaction))))
    assert (row.payee, row.memo) == ("B&LTD SHOP", "A & B")


def test_xml_without_an_encoding_is_utf8() -> None:
    """Fourth review: XML's default encoding is UTF-8; it was read as cp1252 (mojibake)."""
    content = XML_V2.replace(b'encoding="UTF-8" ', b"").replace(
        b"Example Cafe", "Café · £5".encode()
    )
    assert b"encoding=" not in content
    rows = _rows(_parse(content))
    assert rows[0].payee == "Café · £5"


def test_repeated_statement_aggregate_is_file_error() -> None:
    """Fourth review: a second LEDGERBAL was ignored, first-wins."""
    extra = (
        "<LEDGERBAL>\n<BALAMT>100.00\n<DTASOF>20261130\n</LEDGERBAL>\n"
        "<LEDGERBAL>\n<BALAMT>999.00\n<DTASOF>20261130\n</LEDGERBAL>"
    )
    with pytest.raises(FileFormatError, match="has <LEDGERBAL> more than once"):
        _parse(_sgml(_sgml_statement(SGML_TRANSACTIONS, extra=extra)))


def test_unclosed_cdata_is_file_error_and_linear() -> None:
    """Fourth review: the CDATA pre-pass was quadratic on unclosed sections."""
    content = (SGML_HEADER + "<OFX>\n" + "<![CDATA[" * 200_000).encode()
    started = time.perf_counter()
    with pytest.raises(FileFormatError, match="CDATA section is never closed"):
        _parse(content)
    assert time.perf_counter() - started < 2
