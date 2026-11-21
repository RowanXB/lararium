import re
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
    rows = [
        spec
        for s in sections
        for a in (A1, A2)
        for spec in (
            # routed record types have no element of their own
            RowSpec("Order", a, levelOfDetail="ORDER")
            if s == "TradeDetail"
            else RowSpec("Lot", a, container="OpenPositions", levelOfDetail="LOT")
            if s == "PositionLot"
            else RowSpec(s, a),
        )
    ]
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


def test_derivable_sections_are_read_and_unknown_ones_flagged() -> None:
    # Every field the API returns is stored, derivable or not: these sections are read, and
    # one that has never had rows is flagged once it has some, like any unknown section.
    statement = parse_statement(
        build(
            RowSpec("StatementOfFundsLine", A1, amount="1"),
            RowSpec("ChangeInNAV", A1, twr="0.5"),
            RowSpec("MTMPerformanceSummaryUnderlying", A1),
            RowSpec(
                "FIFOPerformanceSummaryUnderlying", A1, container="FIFOPerformanceSummaryInBase"
            ),
            accounts=ACCOUNTS[:1],
        )
    )
    assert statement.flagged == {"FIFOPerformanceSummaryUnderlying"}
    assert {r.section for r in statement.rows} == {
        "AccountInformation",
        "StatementOfFundsLine",
        "ChangeInNAV",
        "MTMPerformanceSummaryUnderlying",
    }


def test_a_period_row_repeated_unchanged_stays_one_per_day() -> None:
    # The owner's MTM Total row is the same on every quiet day: it holds for each day's
    # statement, not for a period spanning them, so it is not merged.
    statement = parse_statement(
        build(
            RowSpec("MTMPerformanceSummaryUnderlying", A1, reportDate="", total="0"),
            RowSpec("CashTransaction", A1),
            accounts=ACCOUNTS[:1],
            by_day=True,
        )
    )
    mtm = [r for r in statement.rows if r.section == "MTMPerformanceSummaryUnderlying"]
    assert sorted((r.period_from, r.period_to) for r in mtm) == [
        (date(2026, 3, d), date(2026, 3, d)) for d in (2, 3, 4)
    ]
    # Other rows repeated unchanged are still one row.
    assert len([r for r in statement.rows if r.section == "CashTransaction"]) == 1


def test_summary_rows_skipped() -> None:
    body = build(
        RowSpec("CashTransaction", A1, levelOfDetail="SUMMARY", transactionID=None),
        RowSpec(
            "CashReportCurrency",
            A1,
            levelOfDetail="BaseCurrency",
            currency="BASE_SUMMARY",
            startingCash="0",
            endingCash="0",
        ),
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


@pytest.mark.parametrize(
    "body",
    [
        b'<FlexQueryResponse queryName="q"><FlexStatements count="0"/></FlexQueryResponse>',
        b"<FlexQueryResponse/>",
        build_statement([], period=PERIOD, rows=[]),
    ],
)
def test_a_response_without_statements_is_refused(body: bytes) -> None:
    with pytest.raises(FlexDataError, match="the Flex query returned no statements"):
        parse_statement(body)


def test_a_statement_without_account_information_is_refused() -> None:
    # Without it no account is created and nothing is posted: taken as a success, a first
    # sync would move its cursor past a year it never read.
    body = re.sub(rb"<AccountInformation [^>]*/>", b"", build(RowSpec("Trade", A1), by_day=True))
    assert b"<AccountInformation" not in body
    with pytest.raises(
        FlexDataError, match=r"^AccountInformation is missing: select it in the Flex query$"
    ):
        parse_statement(body)


def test_a_document_that_is_not_a_flex_response_is_refused() -> None:
    body = build(RowSpec("Trade", A1)).replace(b"FlexQueryResponse", b"SomethingElse")
    with pytest.raises(FlexDataError, match="not a Flex statement"):
        parse_statement(body)


def test_not_xml_refused() -> None:
    with pytest.raises(FlexDataError):
        parse_statement(b"<oops")


PERSONAL_VALUES = {
    "AccountInformation": {
        "name": "Zed Quux",
        "primaryEmail": "zed@example.invalid",
        "dateOfBirth": "19700101",
        "street": "1 Nowhere Rd",
        "street2": "Flat 9",
        "city": "Faketown",
        "postalCode": "ZZ9 9ZZ",
        "accountRepName": "Rep Person",
        "accountRepPhone": "+000000000",
        "beneficiaryDesignations": "Heir Name",
    },
    "Transfer": {"accountName": "Zed Quux ISA"},
}


def _personal(section: str) -> dict[str, str]:
    return PERSONAL_VALUES[section]


def _assert_no_personal(text: str) -> None:
    values = [v for group in PERSONAL_VALUES.values() for v in group.values()]
    assert values and all(v not in text for v in values)


def test_personal_fields_cover_the_fixture() -> None:
    names = {n for group in PERSONAL_VALUES.values() for n in group}
    assert names and names <= PERSONAL_FIELDS
    assert "accountName" in PERSONAL_FIELDS


def test_flags_never_contain_values() -> None:
    body = build(
        RowSpec(
            "AccountInformation", A1, mysteryInfo="Zed Quux", **_personal("AccountInformation")
        ),
        RowSpec("Transfer", A1, mystery="Zed Quux ISA", **_personal("Transfer")),
        accounts=ACCOUNTS[:1],
    )
    statement = parse_statement(body)
    assert statement.flagged == {"AccountInformation.mysteryInfo", "Transfer.mystery"}
    for flag in statement.flagged:
        _assert_no_personal(flag)


@pytest.mark.parametrize(
    ("section", "missing"),
    [("AccountInformation", "dateOpened"), ("Transfer", "transactionID")],
)
def test_error_messages_never_contain_values(section: str, missing: str) -> None:
    body = build(
        RowSpec(section, A1, **{missing: None}, **_personal(section)), accounts=ACCOUNTS[:1]
    )
    with pytest.raises(FlexDataError) as error:
        parse_statement(body)
    assert f"{section}.{missing}" in str(error.value)
    _assert_no_personal(str(error.value))


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_required_attribute_is_missing(value: str) -> None:
    body = build(RowSpec("Transfer", A1, transactionID=value), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError, match=r"Transfer\.transactionID"):
        parse_statement(body)


def test_row_account_must_match_its_statement() -> None:
    body = build(RowSpec("CashTransaction", A1, accountId=A2), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError) as error:
        parse_statement(body)
    assert "CashTransaction.accountId" in str(error.value)
    assert A1 not in str(error.value) and A2 not in str(error.value)


def test_account_information_account_must_match_its_statement() -> None:
    body = build(RowSpec("AccountInformation", A1, accountId=A2), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError, match=r"AccountInformation\.accountId"):
        parse_statement(body)


def test_level_required_in_level_gated_sections() -> None:
    body = build(RowSpec("CashTransaction", A1, levelOfDetail=None), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError, match=r"CashTransaction\.levelOfDetail"):
        parse_statement(body)
    body = build(RowSpec("CashReportCurrency", A1, levelOfDetail=" "), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError, match=r"CashReportCurrency\.levelOfDetail"):
        parse_statement(body)


@pytest.mark.parametrize("section", ["CashTransaction", "CashReportCurrency"])
def test_unknown_level_flagged_and_skipped(section: str) -> None:
    body = build(RowSpec(section, A1, levelOfDetail="BRAND_NEW"), accounts=ACCOUNTS[:1])
    statement = parse_statement(body)
    assert statement.flagged == {f"{section}.levelOfDetail"}
    assert all(r.section != section for r in statement.rows)


def test_cash_held_without_currency_rows_needs_the_currency_breakout() -> None:
    base = {"levelOfDetail": "BaseCurrency", "currency": "BASE_SUMMARY"}
    held = build(RowSpec("CashReportCurrency", A1, **base, endingCash="100"), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError, match="Currency Breakout"):
        parse_statement(held)
    # No cash at all, or the currency rows there: nothing is missing.
    empty = RowSpec("CashReportCurrency", A1, **base, startingCash="0", endingCash="0")
    parse_statement(build(empty, accounts=ACCOUNTS[:1]))
    both = [
        RowSpec("CashReportCurrency", A1, **base, endingCash="100"),
        RowSpec("CashReportCurrency", A1, endingCash="100"),
    ]
    parse_statement(build(*both, accounts=ACCOUNTS[:1]))


def _only(statement, section: str):
    return [r for r in statement.rows if r.section == section]


def test_execution_trade_is_a_trade() -> None:
    statement = parse_statement(build(RowSpec("Trade", A1), accounts=ACCOUNTS[:1]))
    assert len(_only(statement, "Trade")) == 1 and not _only(statement, "TradeDetail")


def test_trade_without_execution_level_is_a_detail() -> None:
    body = build(
        RowSpec("Trade", A1, levelOfDetail="CLOSED_LOT", transactionID=None, tradeID=None),
        accounts=ACCOUNTS[:1],
    )
    statement = parse_statement(body)
    assert len(_only(statement, "TradeDetail")) == 1 and not _only(statement, "Trade")
    assert statement.flagged == frozenset()


@pytest.mark.parametrize(
    ("tag", "level"),
    [
        ("Order", "ORDER"),
        ("Lot", "CLOSED_LOT"),
        ("WashSale", "WASH_SALE"),
        ("SymbolSummary", "SYMBOL_SUMMARY"),
        ("AssetSummary", "ASSET_SUMMARY"),
    ],
)
def test_trade_detail_tags(tag: str, level: str) -> None:
    body = build(RowSpec(tag, A1, levelOfDetail=level, symbol="TST"), accounts=ACCOUNTS[:1])
    statement = parse_statement(body)
    (row,) = _only(statement, "TradeDetail")
    assert row.attributes["levelOfDetail"] == level
    assert statement.flagged == frozenset()


def test_trade_detail_requires_account_and_level_only() -> None:
    body = build(RowSpec("Order", A1, levelOfDetail=None), accounts=ACCOUNTS[:1])
    with pytest.raises(FlexDataError, match=r"TradeDetail\.levelOfDetail"):
        parse_statement(body)


def test_lot_under_open_positions_is_a_position_lot() -> None:
    body = build(
        RowSpec("Lot", A1, container="OpenPositions", levelOfDetail="LOT", symbol="TST"),
        accounts=ACCOUNTS[:1],
    )
    statement = parse_statement(body)
    assert len(_only(statement, "PositionLot")) == 1 and not _only(statement, "TradeDetail")
    assert statement.flagged == frozenset()
    assert SECTIONS["PositionLot"].record_type == "ibkr.position_lot"
    assert SECTIONS["TradeDetail"].record_type == "ibkr.trade_detail"


def test_skipped_row_may_carry_another_account() -> None:
    body = build(
        RowSpec("CashTransaction", A1, levelOfDetail="SUMMARY", accountId=A2, transactionID=None),
        accounts=ACCOUNTS[:1],
    )
    assert parse_statement(body).flagged == frozenset()
