"""IBKR Flex: the ibkr_account registry kind and the typed tables of what a statement says.

Every field of the Flex query that the ledger has no home for is a typed column of one of the
finance.ibkr_* tables (columns are IBKR's attribute names in snake_case); see the IBKR Flex
design, section 5.9. Amounts are numeric, dates date, instants timestamptz and codes text.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-19
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
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


# Trade fields kept for an execution (its ledger row holds the rest) and for the other levels.
_TRADE_TEXT = """tradeID ibOrderID ibExecID brokerageOrderID orderReference exchange buySell
openCloseIndicator orderType notes transactionType origTradeID origOrderID origTransactionID
clearingFirmID relatedTransactionID rtn volatilityOrderLink exchOrderId extExecID traderID
isAPIOrder positionActionID code"""
_TRADE_NUMERIC = """fxRateToBase cost fifoPnlRealized fxPnl mtmPnl closePrice origTradePrice
changeInPrice changeInQuantity accruedInt initialInvestment"""
_TRADE_DATE = "tradeDate settleDateTarget origTradeDate"
_TRADE_INSTANT = "orderTime openDateTime holdingPeriodDateTime whenRealized whenReopened"

_POSITION_TEXT = "side levelOfDetail code originatingOrderID"
_POSITION_NUMERIC = """fxRateToBase markPrice openPrice costBasisPrice costBasisMoney
fifoPnlUnrealized percentOfNAV accruedInt unrealizedCapitalGainsPnl unrealizedlFxPnl"""
_POSITION_INSTANT = "openDateTime holdingPeriodDateTime"

_CASH_REPORT_NUMERIC = """startingCash endingCash endingSettledCash deposits withdrawals
depositWithdrawals internalTransfers accountTransfers commissions dividends brokerInterest
brokerFees otherFees otherIncome withholdingTax paymentInLieu transactionTax netTradesSales
netTradesPurchases fxTranslationGainLoss advisorFees billPay billableCommissions
billableSalesTax bondInterest carbonCredits cashSettlingMtm cfdCharges clientFees
commissionCreditsRedemption debitCardActivity donations excessFundSweep feesReceivables
insuredDepositInterest ipoSubscription linkingAdjustments other paxosTransfers realizedForexVm
realizedVm referralFee salesTax slbEndingCashCollateral slbNetCash
slbNetSecuritiesLentActivity slbNetSettledCash slbStartingCashCollateral taxReceivables
withholding871m withholdingCollectedTax"""

_NAV_NUMERIC = """total cash stock funds bonds options commodities notes crypto interestAccruals
dividendAccruals cgtWithholdingAccruals bondInterestAccrualsComponent brokerCashComponent
brokerFeesAccrualsComponent brokerInterestAccrualsComponent cfdUnrealizedPl
eventContractInterestAccruals fdicInsuredAccountInterestAccrualsComponent
fdicInsuredBankSweepAccountCashComponent forexCfdUnrealizedPl incentiveCouponAccruals
insuredBankDepositRedemptionCashComponent ipoSubscription liteSurchargeAccruals
marginFinancingChargeAccruals physDel providerCryptoCash slbCashCollateral
slbDirectSecuritiesBorrowed slbDirectSecuritiesLent softDollars"""


def _ledger_row_head(name: str) -> list[str]:
    # Keyed by the ledger row it describes; IBKR's transactionID is a text column of its own.
    return [
        "transaction_id uuid PRIMARY KEY REFERENCES finance.transactions (id) ON DELETE CASCADE",
        "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
        "ibkr_transaction_id text NOT NULL",
        f"CONSTRAINT {name}_ibkr_id UNIQUE (account_id, ibkr_transaction_id)",
    ]


_UPGRADE_TABLES = [
    _table(
        "ibkr_accounts",
        [
            "account_id uuid PRIMARY KEY REFERENCES finance.accounts (id)",
            "ibkr_account_id text NOT NULL UNIQUE"
            " CHECK (ibkr_account_id ~ '^[A-Z]{1,3}[0-9]{3,12}$')",
            "base_instrument_id uuid NOT NULL REFERENCES finance.instruments (id)",
        ],
        _columns("date", "dateOpened", "dateOpened"),
        _columns("date", "dateFunded dateClosed lastTradedDate dateOfBirth"),
        _columns(
            "text",
            """acctAlias model accountType customerType accountCapabilities tradingPermissions
            ibEntity dividendReinvestmentEnabled hasPendingTasks taxLotMatchingMethod
            recurringTransactions syepEnrollmentStatus masterName name street street2 city state
            country postalCode streetResidentialAddress street2ResidentialAddress
            cityResidentialAddress stateResidentialAddress countryResidentialAddress
            postalCodeResidentialAddress primaryEmail beneficiaryDesignations accountRepName
            accountRepPhone""",
        ),
    ),
    _table(
        "ibkr_instruments",
        [
            "instrument_id uuid PRIMARY KEY REFERENCES finance.instruments (id)",
            "conid text NOT NULL UNIQUE",
        ],
        _columns("text", "assetCategory", "assetCategory"),
        _columns(
            "text",
            """symbol description securityID securityIDType cusip isin figi listingExchange
            subCategory issuer issuerCountryCode underlyingConid underlyingSymbol
            underlyingSecurityID underlyingListingExchange putCall commodityType deliveryType
            serialNumber""",
        ),
        _columns("numeric", "multiplier strike principalAdjustFactor fineness weight"),
        _columns("date", "expiry"),
    ),
    _table(
        "ibkr_trades",
        _ledger_row_head("ibkr_trades"),
        _columns("text", f"levelOfDetail {_TRADE_TEXT}"),
        _columns("numeric", _TRADE_NUMERIC),
        _columns("date", _TRADE_DATE),
        _columns("timestamptz", _TRADE_INSTANT),
    ),
    _table(
        "ibkr_cash_transactions",
        [
            *_ledger_row_head("ibkr_cash_transactions"),
            "instrument_id uuid REFERENCES finance.instruments (id)",
        ],
        _columns(
            "text",
            "type code tradeID actionID dividendType clientReference levelOfDetail",
            "type",
        ),
        _columns("numeric", "fxRateToBase"),
        _columns("date", "settleDate availableForTradingDate exDate"),
    ),
    _table(
        "ibkr_corporate_actions",
        _ledger_row_head("ibkr_corporate_actions"),
        _columns("text", "type actionID code actionDescription levelOfDetail"),
        _columns("numeric", "value amount fifoPnlRealized mtmPnl fxRateToBase"),
    ),
    _table(
        "ibkr_transfers",
        _ledger_row_head("ibkr_transfers"),
        _columns(
            "text",
            """type direction company account accountName deliveringBroker
            positionInstructionID positionInstructionSetID clientReference code levelOfDetail""",
        ),
        _columns(
            "numeric",
            """cashTransfer transferPrice positionAmount positionAmountInBase pnlAmount
            pnlAmountInBase cost fxRateToBase""",
        ),
        _columns("date", "date settleDate"),
        _columns("timestamptz", "openDateTime"),
    ),
    _table(
        "ibkr_positions",
        [
            "snapshot_id uuid PRIMARY KEY REFERENCES finance.balance_snapshots (id)"
            " ON DELETE CASCADE",
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
        ],
        _columns("text", f"{_POSITION_TEXT} originatingTransactionID"),
        _columns("numeric", _POSITION_NUMERIC),
        _columns("timestamptz", _POSITION_INSTANT),
        _columns("date", "vestingDate"),
    ),
    _table(
        "ibkr_position_lots",
        [
            "snapshot_id uuid NOT NULL REFERENCES finance.balance_snapshots (id) ON DELETE CASCADE",
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
            "originating_transaction_id text NOT NULL",
        ],
        _columns("text", _POSITION_TEXT),
        _columns("numeric", f"position {_POSITION_NUMERIC}"),
        _columns("timestamptz", _POSITION_INSTANT),
        _columns("date", "vestingDate"),
        tail=["PRIMARY KEY (snapshot_id, originating_transaction_id)"],
    ),
    _table(
        "ibkr_trade_details",
        [
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
            "level_of_detail text NOT NULL CHECK (level_of_detail <> 'EXECUTION')",
            # transactionID, else tradeID, else a hash of the attributes.
            "row_key text NOT NULL",
            "instrument_id uuid REFERENCES finance.instruments (id)",
        ],
        _columns("text", f"currency ibCommissionCurrency transactionID {_TRADE_TEXT}"),
        _columns("numeric", _TRADE_NUMERIC),
        _columns(
            "numeric",
            "quantity tradePrice tradeMoney proceeds taxes ibCommission netCash netCashInBase",
        ),
        _columns("date", f"reportDate {_TRADE_DATE}"),
        _columns("timestamptz", f"dateTime {_TRADE_INSTANT}"),
        tail=["PRIMARY KEY (account_id, level_of_detail, row_key)"],
    ),
    _table(
        "ibkr_cash_reports",
        [
            "id uuid PRIMARY KEY DEFAULT uuidv7()",
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
            "currency_instrument_id uuid NOT NULL REFERENCES finance.instruments (id)",
        ],
        _columns("date", "fromDate toDate", "fromDate toDate"),
        _columns("text", "levelOfDetail"),
        _columns("numeric", _CASH_REPORT_NUMERIC, "endingCash"),
        tail=[
            "UNIQUE (account_id, currency_instrument_id, from_date, to_date)",
            "CHECK (from_date <= to_date)",
        ],
    ),
    _table(
        "ibkr_nav",
        [
            "id uuid PRIMARY KEY DEFAULT uuidv7()",
            "account_id uuid NOT NULL REFERENCES finance.accounts (id)",
        ],
        _columns("date", "reportDate", "reportDate"),
        _columns("numeric", _NAV_NUMERIC, "total"),
        tail=["UNIQUE (account_id, report_date)"],
    ),
]

_DROP_IDENTIFIER_CHECKS = """
DO $$
DECLARE c record;
BEGIN
  FOR c IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = 'finance.account_identifiers'::regclass
      AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%uk_bank_account%'
  LOOP
    EXECUTE format('ALTER TABLE finance.account_identifiers DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$
"""

UPGRADE = [
    _DROP_IDENTIFIER_CHECKS,
    """
    ALTER TABLE finance.account_identifiers
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account')),
      ADD CONSTRAINT account_identifiers_uk_bank_account_check
        CHECK (kind <> 'uk_bank_account' OR value ~ '^[0-9]{6}:[0-9]{8}$'),
      ADD CONSTRAINT account_identifiers_ibkr_account_check
        CHECK (kind <> 'ibkr_account' OR value ~ '^[A-Z]{1,3}[0-9]{3,12}$')
    """,
    *_UPGRADE_TABLES,
]

DOWNGRADE = [
    *(
        f"DROP TABLE finance.{name}"
        for name in (
            "ibkr_nav",
            "ibkr_cash_reports",
            "ibkr_trade_details",
            "ibkr_position_lots",
            "ibkr_positions",
            "ibkr_transfers",
            "ibkr_corporate_actions",
            "ibkr_cash_transactions",
            "ibkr_trades",
            "ibkr_instruments",
            "ibkr_accounts",
        )
    ),
    "DELETE FROM finance.account_identifiers WHERE kind = 'ibkr_account'",
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_kind_check,
      DROP CONSTRAINT account_identifiers_uk_bank_account_check,
      DROP CONSTRAINT account_identifiers_ibkr_account_check,
      ADD CONSTRAINT account_identifiers_kind_check CHECK (kind IN ('uk_bank_account')),
      ADD CONSTRAINT account_identifiers_check
        CHECK (kind <> 'uk_bank_account' OR value ~ '^[0-9]{6}:[0-9]{8}$')
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
