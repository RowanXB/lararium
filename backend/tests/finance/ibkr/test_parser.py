from datetime import date

import pytest

from lararium.modules.finance.ibkr.fields import PERSONAL_FIELDS, SECTIONS
from lararium.modules.finance.ibkr.parser import FlexDataError, parse_statement
from tests.finance.ibkr.fixtures import AccountSpec, RowSpec, build_statement

A1 = "U0000001"
A2 = "U0000002"
ACCOUNTS = [AccountSpec(A1), AccountSpec(A2, base="USD")]
PERIOD = (date(2026, 3, 2), date(2026, 3, 4))


def build(*rows: RowSpec, accounts: list[AccountSpec] | None = None, by_day: bool = False):
    return build_statement(accounts or ACCOUNTS, period=PERIOD, rows=rows, by_day=by_day)


def test_parses_every_section() -> None:
    sections = [s for s in SECTIONS if s != "AccountInformation"]
    rows = [RowSpec(s, a) for s in sections for a in (A1, A2)]
    statement = parse_statement(build(*rows))
    assert statement.flagged == frozenset()
    for section in sections:
        got = [r for r in statement.rows if r.section == section]
        # Conversion rates collapse across accounts; every other section keeps one per account.
        assert len(got) == (1 if section == "ConversionRate" else 2), section
    for row in statement.rows:
        assert row.period_from == PERIOD[0] and row.period_to == PERIOD[1]
    trade = next(r for r in statement.rows if r.section == "Trade" and r.account == A2)
    assert trade.attributes["accountId"] == A2 and trade.attributes["currency"] == "USD"
    assert {r.account for r in statement.rows if r.section == "AccountInformation"} == {A1, A2}


def test_missing_required_field_names_it() -> None:
    body = build(
        RowSpec("Trade", A1, transactionID=None, tradePrice="123.456"), accounts=ACCOUNTS[:1]
    )
    with pytest.raises(FlexDataError) as error:
        parse_statement(body)
    assert "Trade.transactionID" in str(error.value)
    assert "123.456" not in str(error.value)


def test_unknown_attribute_kept_and_flagged() -> None:
    body = build(RowSpec("CashTransaction", A1, brandNewField="x"), accounts=ACCOUNTS[:1])
    statement = parse_statement(body)
    row = next(r for r in statement.rows if r.section == "CashTransaction")
    assert row.attributes["brandNewField"] == "x"
    assert statement.flagged == {"CashTransaction.brandNewField"}


def test_empty_unknown_attribute_ignored() -> None:
    body = build(RowSpec("CashTransaction", A1, brandNewField=""), accounts=ACCOUNTS[:1])
    statement = parse_statement(body)
    row = next(r for r in statement.rows if r.section == "CashTransaction")
    assert "brandNewField" not in row.attributes
    assert statement.flagged == frozenset()


def test_unknown_section_with_rows_flagged() -> None:
    statement = parse_statement(build(RowSpec("Mystery", A1, a="1"), accounts=ACCOUNTS[:1]))
    assert statement.flagged == {"Mystery"}
    assert all(r.section != "Mystery" for r in statement.rows)


def test_derivable_section_skipped_silently() -> None:
    statement = parse_statement(
        build(RowSpec("StatementOfFundsLine", A1, amount="1"), accounts=ACCOUNTS[:1])
    )
    assert statement.flagged == frozenset()
    assert {r.section for r in statement.rows} == {"AccountInformation"}


def test_summary_rows_skipped() -> None:
    body = build(
        RowSpec("CashTransaction", A1, levelOfDetail="SUMMARY", transactionID=None),
        RowSpec("CashReportCurrency", A1, levelOfDetail="BaseCurrency", currency="BASE_SUMMARY"),
        accounts=ACCOUNTS[:1],
    )
    statement = parse_statement(body)
    assert {r.section for r in statement.rows} == {"AccountInformation"}
    assert statement.flagged == frozenset()


def test_breakout_by_day_merged() -> None:
    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]
    rows = [RowSpec("ConversionRate", a, day=d) for a in (A1, A2) for d in days]
    rows += [RowSpec("ConversionRate", a, fromCurrency="EUR") for a in (A1, A2)]
    rows += [RowSpec("CashTransaction", a, transactionID="8000001") for a in (A1, A2)]
    rows += [RowSpec("CashTransaction", A1, transactionID="8000002", day=days[1])]
    statement = parse_statement(build(*rows, by_day=True))
    rates = [r for r in statement.rows if r.section == "ConversionRate"]
    # three dated USD rates, plus one EUR rate repeated in every daily statement of both accounts
    assert len(rates) == 4
    keys = {(r.attributes["reportDate"], r.attributes["fromCurrency"]) for r in rates}
    assert len(keys) == 4
    cash = [r for r in statement.rows if r.section == "CashTransaction"]
    assert sorted((r.account, r.attributes["transactionID"]) for r in cash) == [
        (A1, "8000001"),
        (A1, "8000002"),
        (A2, "8000001"),
    ]
    merged = next(r for r in cash if r.attributes["transactionID"] == "8000001" and r.account == A1)
    assert (merged.period_from, merged.period_to) == (days[0], days[-1])
    assert len([r for r in statement.rows if r.section == "AccountInformation"]) == 2


def test_account_information_keeps_latest_statement() -> None:
    body = build(
        RowSpec("AccountInformation", A1, accountType="Individual"),
        accounts=[AccountSpec(A1, date_closed=date(2026, 3, 4))],
        by_day=True,
    )
    (info,) = parse_statement(body).rows
    assert info.period_to == PERIOD[1] and info.attributes["dateClosed"] == "20260304"


def test_single_row_section_attributes() -> None:
    body = build(
        RowSpec("AccountInformation", A1, name="Test Person", dateOpened="20200115"),
        accounts=ACCOUNTS[:1],
    )
    (info,) = parse_statement(body).rows
    assert info.section == "AccountInformation" and info.account == A1
    assert info.attributes["dateOpened"] == "20200115"
    assert info.attributes["name"] == "Test Person"


def test_doctype_refused() -> None:
    body = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "v">]><FlexQueryResponse/>'
    with pytest.raises(FlexDataError, match="DOCTYPE"):
        parse_statement(body)


def test_not_xml_refused() -> None:
    with pytest.raises(FlexDataError):
        parse_statement(b"<oops")


def test_flags_never_contain_values() -> None:
    personal = {
        name: f"secret-{name}" for name in PERSONAL_FIELDS if name in SECTIONS["Transfer"].known
    }
    body = build(
        RowSpec("Transfer", A1, mystery="secret-mystery", **personal),
        RowSpec("Transfer", A1, transactionID=None, **personal),
        accounts=ACCOUNTS[:1],
    )
    with pytest.raises(FlexDataError) as error:
        parse_statement(body)
    assert "secret-" not in str(error.value)
    flagged_body = build(
        RowSpec("Transfer", A1, mystery="secret-mystery", **personal), accounts=ACCOUNTS[:1]
    )
    statement = parse_statement(flagged_body)
    assert statement.flagged == {"Transfer.mystery"}
    assert not any("secret-" in flag for flag in statement.flagged)
