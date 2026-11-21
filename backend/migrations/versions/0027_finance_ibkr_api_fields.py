"""IBKR Flex: a typed column for every field the API returns, derivable or not (owner,
2026-11-21).

New tables for the sections that were skipped as derivable: Change in NAV, the Statement of
Funds and the MTM Performance Summary by underlying; and one for Prior Period Positions, whose
price alone was kept. New columns for the derivable fields that were dropped: a trade's
tradeMoney, netCash and netCashInBase, a position's and a lot's positionValue and
positionValueInBase. Columns are IBKR's attribute names in snake_case, as in 0019.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-21
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NON_FINITE = "('NaN', 'Infinity', '-Infinity')"


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def _columns(kind: str, names: str, required: str = "") -> list[str]:
    """Column definitions for IBKR attribute names of one kind; `required` ones are NOT NULL."""
    needed = {_snake(n) for n in required.split()}
    lines: list[str] = []
    for attribute in names.split():
        column = _snake(attribute)
        line = f'"{column}" {kind}'
        if column in needed:
            line += " NOT NULL"
        if kind == "numeric":
            line += f' CHECK ("{column}" NOT IN {_NON_FINITE})'
        lines.append(line)
    return lines


def _table(name: str, head: list[str], *groups: list[str], tail: Sequence[str] = ()) -> str:
    body = ",\n      ".join([*head, *(line for group in groups for line in group), *tail])
    return f"CREATE TABLE finance.{name} (\n      {body}\n    )"


# What identifies a security, where a row names one (as ibkr_instruments has it).
_INSTRUMENT_TEXT = """assetCategory conid symbol description securityID securityIDType cusip
isin figi listingExchange subCategory issuer issuerCountryCode underlyingConid underlyingSymbol
underlyingSecurityID underlyingListingExchange putCall commodityType deliveryType serialNumber"""
_INSTRUMENT_NUMERIC = "multiplier strike principalAdjustFactor fineness weight"

_CHANGE_IN_NAV_NUMERIC = """startingValue endingValue mtm realized changeInUnrealized
costAdjustments transferredPnlAdjustments depositsWithdrawals internalCashTransfers
assetTransfers linkingAdjustments debitCardActivity billPay dividends withholdingTax
withholding871m withholdingTaxCollected changeInDividendAccruals interest
changeInInterestAccruals changeInBrokerFeeAccruals changeInCGTWithholdingAccruals
changeInIncentiveCouponAccruals changeInLiteSurchargeAccruals advisorFees clientFees otherFees
brokerFees feesReceivables commissions commissionReceivables commissionCreditsRedemption
commissionsAtPaxos forexCommissions transactionTax taxReceivables salesTax billableSalesTax
softDollars netFxTrading fxTranslation corporateActionProceeds grantActivity carbonCredits
donations excessFundSweep mtmAtPaxos paxosTransfers referralFee otherIncome other twr"""

# The period of the statement the row is in: a Statement of Funds' balance and an MTM summary
# hold for that period only.
_PERIOD_HEAD = [
    "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
    "from_date date NOT NULL",
    "to_date date NOT NULL",
    "row_key text NOT NULL",
]
_PERIOD_TAIL = [
    "PRIMARY KEY (account_id, from_date, to_date, row_key)",
    "CHECK (from_date <= to_date)",
]

_TABLES = [
    _table(
        "ibkr_change_in_nav",
        [
            "id uuid PRIMARY KEY DEFAULT uuidv7()",
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
            "currency_instrument_id uuid NOT NULL REFERENCES finance.instruments (id)",
        ],
        _columns("date", "fromDate toDate", "fromDate toDate"),
        _columns("numeric", _CHANGE_IN_NAV_NUMERIC),
        tail=["UNIQUE (account_id, from_date, to_date)", "CHECK (from_date <= to_date)"],
    ),
    _table(
        "ibkr_statement_of_funds",
        [
            *_PERIOD_HEAD,
            "currency_instrument_id uuid NOT NULL REFERENCES finance.instruments (id)",
        ],
        _columns("text", "levelOfDetail activityDescription", "levelOfDetail activityDescription"),
        _columns(
            "text",
            f"""activityCode buySell tradeCode transactionID tradeID orderID actionID
            relatedTradeID relatedTransactionID origTransactionID {_INSTRUMENT_TEXT}""",
        ),
        _columns(
            "numeric",
            f"""amount balance credit debit tradeCommission tradeGross tradePrice tradeQuantity
            tradeTax fxRateToBase {_INSTRUMENT_NUMERIC}""",
        ),
        _columns("date", "date reportDate settleDate expiry"),
        tail=_PERIOD_TAIL,
    ),
    _table(
        "ibkr_mtm_performance",
        _PERIOD_HEAD,
        _columns("text", f"code {_INSTRUMENT_TEXT}"),
        _columns(
            "numeric",
            f"""prevCloseQuantity prevClosePrice closeQuantity closePrice transactionMtm
            priorOpenMtm commissions other otherWithAccruals total totalWithAccruals
            {_INSTRUMENT_NUMERIC}""",
        ),
        _columns("date", "reportDate expiry"),
        tail=_PERIOD_TAIL,
    ),
    _table(
        "ibkr_prior_period_positions",
        [
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
            "instrument_id uuid NOT NULL REFERENCES finance.instruments (id)",
            "currency_instrument_id uuid NOT NULL REFERENCES finance.instruments (id)",
        ],
        _columns("date", "date", "date"),
        _columns("text", "levelOfDetail"),
        _columns("numeric", "price priorMtmPnl fxRateToBase", "price"),
        _columns("date", "reportDate"),
        tail=["PRIMARY KEY (account_id, instrument_id, date)"],
    ),
]


def _add_numeric(table: str, names: str) -> str:
    added = ",\n      ".join(f"ADD COLUMN {line}" for line in _columns("numeric", names))
    return f"ALTER TABLE finance.{table}\n      {added}"


_ADDED = {
    "ibkr_trades": "tradeMoney netCash netCashInBase",
    "ibkr_positions": "positionValue positionValueInBase",
    "ibkr_position_lots": "positionValue positionValueInBase",
}


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    for table, names in _ADDED.items():
        op.execute(_add_numeric(table, names))


def downgrade() -> None:
    for table, names in _ADDED.items():
        dropped = ",\n      ".join(f'DROP COLUMN "{_snake(n)}"' for n in names.split())
        op.execute(f"ALTER TABLE finance.{table}\n      {dropped}")
    for name in (
        "ibkr_prior_period_positions",
        "ibkr_mtm_performance",
        "ibkr_statement_of_funds",
        "ibkr_change_in_nav",
    ):
        op.execute(f"DROP TABLE finance.{name}")
