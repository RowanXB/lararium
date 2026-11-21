"""A builder of synthetic Flex statement XML: invented ids and values only."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from xml.etree import ElementTree

from lararium.modules.finance.ibkr.fields import SECTIONS, SINGLE_ROW_SECTIONS

# Container element of each row element; a section not listed gets "<Section>s".
CONTAINERS: Mapping[str, str] = {
    "OpenPosition": "OpenPositions",
    "PriorPeriodPosition": "PriorPeriodPositions",
    "CashReportCurrency": "CashReport",
    "Trade": "Trades",
    "CashTransaction": "CashTransactions",
    "ConversionRate": "ConversionRates",
    "CorporateAction": "CorporateActions",
    "Transfer": "Transfers",
    "EquitySummaryByReportDateInBase": "EquitySummaryInBase",
    "StatementOfFundsLine": "StmtFunds",
    "MTMPerformanceSummaryUnderlying": "MTMPerformanceSummaryInBase",
    "Order": "Trades",
    "Lot": "Trades",
    "WashSale": "Trades",
    "SymbolSummary": "Trades",
    "AssetSummary": "Trades",
}


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    base: str = "GBP"
    date_opened: date = date(2020, 1, 15)
    date_closed: date | None = None


class RowSpec:
    """One row. `attributes` override the defaults; a value of None drops the attribute.

    `container` overrides the container element (a Lot sits under Trades or OpenPositions).
    `day` puts the row in that day's statement only when the statement is built by day;
    without it the row is repeated in every daily statement, as IBKR repeats conversion rates.
    """

    def __init__(
        self,
        section: str,
        account_id: str,
        *,
        day: date | None = None,
        container: str | None = None,
        **attributes: str | None,
    ) -> None:
        self.section = section
        self.account_id = account_id
        self.day = day
        self.container = container
        self.attributes = attributes


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _defaults(section: str, account: AccountSpec, end: date) -> dict[str, str]:
    base: dict[str, str] = {
        "accountId": account.account_id,
        "currency": account.base,
        "reportDate": _ymd(end),
    }
    moment = f"{_ymd(end)};093000 EST"
    extra: dict[str, dict[str, str]] = {
        "AccountInformation": {
            "dateOpened": _ymd(account.date_opened),
            "customerType": "Individual",
        },
        "OpenPosition": {"position": "10", "markPrice": "100", "levelOfDetail": "SUMMARY"},
        "PriorPeriodPosition": {"date": _ymd(end), "price": "99"},
        "CashReportCurrency": {
            "fromDate": _ymd(end),
            "toDate": _ymd(end),
            "startingCash": "0",
            "endingCash": "100",
            "levelOfDetail": "Currency",
        },
        "Trade": {
            "transactionID": "9000001",
            "dateTime": moment,
            "quantity": "10",
            "tradePrice": "100",
            "proceeds": "-1000",
            "ibCommission": "-1",
            "ibCommissionCurrency": account.base,
            "levelOfDetail": "EXECUTION",
        },
        "CashTransaction": {
            "transactionID": "8000001",
            "dateTime": moment,
            "amount": "100",
            "type": "Deposits/Withdrawals",
            "levelOfDetail": "DETAIL",
        },
        "ConversionRate": {"fromCurrency": "USD", "toCurrency": "GBP", "rate": "0.8"},
        "CorporateAction": {
            "transactionID": "7000001",
            "dateTime": moment,
            "quantity": "1",
            "proceeds": "0",
        },
        "Transfer": {
            "transactionID": "6000001",
            "dateTime": moment,
            "quantity": "1",
            "type": "INTERNAL",
            "levelOfDetail": "TRANSFER",
        },
        "EquitySummaryByReportDateInBase": {"total": "1000"},
        "ChangeInNAV": {"fromDate": _ymd(end), "toDate": _ymd(end), "endingValue": "1000"},
        "StatementOfFundsLine": {
            "levelOfDetail": "Currency",
            "activityDescription": "Starting Balance",
            "date": _ymd(end),
            "balance": "100",
        },
        "MTMPerformanceSummaryUnderlying": {"description": "Total", "total": "0"},
    }
    merged = {**base, **extra.get(section, {})}
    if section == "ConversionRate":
        for name in ("accountId", "currency"):
            merged.pop(name)
    if section in ("CashReportCurrency", "ChangeInNAV"):
        merged.pop("reportDate")
    if section == "MTMPerformanceSummaryUnderlying":
        merged.pop("currency")
    return merged


def _element(
    parent: ElementTree.Element, tag: str, attributes: Mapping[str, str | None]
) -> ElementTree.Element:
    return ElementTree.SubElement(
        parent, tag, {k: v for k, v in attributes.items() if v is not None}
    )


def _days(period: tuple[date, date]) -> list[date]:
    start, end = period
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def build_statement(
    accounts: Sequence[AccountSpec],
    *,
    period: tuple[date, date],
    rows: Sequence[RowSpec],
    by_day: bool = False,
    empty_sections: Sequence[str] = (),
) -> bytes:
    """Valid synthetic Flex XML. With by_day, one FlexStatement per account per day.
    `empty_sections` are container elements listed in every statement even without rows."""
    windows = [(day, day) for day in _days(period)] if by_day else [period]
    root = ElementTree.Element("FlexQueryResponse", {"queryName": "synthetic", "type": "AF"})
    statements = ElementTree.SubElement(root, "FlexStatements")
    for account in accounts:
        for start, end in windows:
            statement = ElementTree.SubElement(
                statements,
                "FlexStatement",
                {
                    "accountId": account.account_id,
                    "fromDate": _ymd(start),
                    "toDate": _ymd(end),
                    "period": "Daily" if by_day else "Custom",
                    "whenGenerated": f"{_ymd(end)};120000 EST",
                },
            )
            info = _defaults("AccountInformation", account, end)
            info.pop("reportDate")
            if account.date_closed is not None:
                info["dateClosed"] = _ymd(account.date_closed)
            for spec in rows:
                if spec.section == "AccountInformation" and spec.account_id == account.account_id:
                    info.update(spec.attributes)
            _element(statement, "AccountInformation", info)
            containers: dict[str, ElementTree.Element] = {
                name: ElementTree.SubElement(statement, name) for name in empty_sections
            }
            for spec in rows:
                if spec.account_id != account.account_id or spec.section == "AccountInformation":
                    continue
                if spec.day is not None and by_day and spec.day != start:
                    continue
                if spec.section in SINGLE_ROW_SECTIONS:
                    # The section element is the row (Change in NAV, one per statement).
                    defaults = _defaults(spec.section, account, spec.day or period[1])
                    _element(statement, spec.section, {**defaults, **spec.attributes})
                    continue
                name = spec.container or CONTAINERS.get(spec.section, f"{spec.section}s")
                if name not in containers:
                    containers[name] = ElementTree.SubElement(statement, name)
                values: dict[str, str | None] = dict(
                    _defaults(spec.section, account, spec.day or period[1])
                    if spec.section in SECTIONS
                    else {"accountId": account.account_id}
                )
                values.update(spec.attributes)
                _element(containers[name], spec.section, values)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
