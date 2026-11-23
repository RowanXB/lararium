"""eToro: the etoro_account registry kind, the typed tables of what the API and the account
statement return, review items and owner decisions between two sources, and the fee a
transfer leg carries.

Every known path of the eToro field lists (etoro/fields.py) is a typed column of a
finance.etoro_* table, named by the snake_case of its dotted path below the row, and every
known statement column by the snake_case of its header (M6 design, Decision 3); an array of
objects is a child table keyed by its parent and the item's index. Numbers, ids among them,
are numeric; strings text; instants timestamptz (a zone-less one timestamp); dates date; flags
boolean. eToro's own accountId and instrumentId are fields (account_id, instrument_id), so a
row's links to finance's accounts and instruments are finance_account_id and
finance_instrument_id. A row that is posted links its ledger row, which it outlives (SET NULL).

finance.source_reviews and finance.source_decisions (any connector's; Decision 14): a value
two sources of one event disagree on, open until the owner takes one; the owner's take, which
regeneration replays. finance.transfer_leg_fees (Decision 15): the conversion fee a
connector supplies for a transfer leg.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-23
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NON_FINITE = "('NaN', 'Infinity', '-Infinity')"
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _column(path: str) -> str:
    """Decision 3's name for a path or a header, as typed_tables.column_name gives it today."""
    return re.sub(r"[^a-z0-9]+", "_", _CAMEL.sub("_", path).lower()).strip("_")


def _line(column: str, kind: str, required: bool) -> str:
    line = f'"{column}" {kind}'
    if required:
        line += " NOT NULL"
    if kind == "numeric":
        line += f' CHECK ("{column}" NOT IN {_NON_FINITE})'
    return line


def _columns(kind: str, paths: str, required: str = "", *, prefix: str = "") -> list[str]:
    """Column definitions for the paths of one type, below `prefix` (an array's item, whose
    path the child table drops); `required` ones are NOT NULL."""
    needed = set(required.split())
    listed = paths.split()
    if unknown := needed - set(listed):
        raise ValueError(f"required but not listed: {sorted(unknown)}")
    return [_line(_column(path.removeprefix(prefix)), kind, path in needed) for path in listed]


def _headers(kind: str, names: str, *, prefix: str = "", suffix: str = "") -> list[str]:
    """Column definitions for statement headers (or labels), `|`-separated."""
    headers = [name.strip() for name in names.replace("\n", "").split("|")]
    return [_line(f"{prefix}{_column(name)}{suffix}", kind, False) for name in headers]


def _table(name: str, head: list[str], *groups: list[str], tail: Sequence[str] = ()) -> str:
    body = ",\n      ".join([*head, *(line for group in groups for line in group), *tail])
    return f"CREATE TABLE finance.{name} (\n      {body}\n    )"


def _under(prefix: str, names: str) -> str:
    return " ".join(f"{prefix}{name}" for name in names.split())


_CONNECTION = "connection_id uuid NOT NULL REFERENCES core.connections (id)"
_RAW = "raw_record_id uuid NOT NULL REFERENCES core.raw_records (id)"
_DAY = "as_of_date date NOT NULL"
_INDEX = "item_index integer NOT NULL CHECK (item_index >= 0)"
_LEDGER = "transaction_id uuid UNIQUE REFERENCES finance.transactions (id) ON DELETE SET NULL"
# A statement row may enrich a record another row posts: several rows, one transaction.
_ENRICHES = "transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL"
_SNAPSHOT = "snapshot_id uuid UNIQUE REFERENCES finance.balance_snapshots (id) ON DELETE SET NULL"
_ACCOUNT = "finance_account_id uuid REFERENCES finance.accounts (id)"
_INSTRUMENT = "finance_instrument_id uuid REFERENCES finance.instruments (id)"
_ROW_KEY = "row_key text NOT NULL CHECK (row_key ~ '^[0-9a-f]{32}$')"


def _child_of(parent: str, columns: str, keys: str) -> str:
    return f"FOREIGN KEY ({columns}) REFERENCES finance.{parent} ({keys}) ON DELETE CASCADE"


# The field lists, as etoro/fields.py declares them (2026-11-23).
_PNL_POSITION_NUMERIC = """CID amount initialAmountInDollars initialUnits instrumentID leverage
lotCount mirrorID openConversionRate openPositionActionType openRate orderID orderType
parentPositionID pnlVersion positionID redeemStatusID settlementTypeID stopLossRate
stopLossVersion takeProfitRate totalExternalFees totalExternalTaxes totalFees units
unitsBaseValueDollars unrealizedPnL.assetCurrencyId unrealizedPnL.closeConversionRate
unrealizedPnL.closeRate unrealizedPnL.exposureInAccountCurrency
unrealizedPnL.exposureInAssetCurrency unrealizedPnL.marginCurrencyId
unrealizedPnL.marginInAccountCurrency unrealizedPnL.marginInAssetCurrency unrealizedPnL.pnL
unrealizedPnL.pnlAssetCurrency"""
_PNL_POSITION_BOOLEAN = """isBuy isDetached isDiscounted isNoStopLoss isNoTakeProfit
isPartiallyAltered isSettled isTslEnabled"""
_ACCOUNT_TOTALS = """accountAvailableCash accountAvailableMargin accountBalance
accountCurrentPnl accountFrozenCash accountTotalUsedMargin accountTotalValue
cryptoAvailableMargin"""
_AGGREGATE_NUMERIC = """accountCurrencyReturn accountCurrencyRoePercent avgConversionRate
avgLeverage avgOpenRate instrumentId liquidationValueAccountCurrency
liquidationValueAssetCurrency loanAmount netAvgOpenRate netContracts
netCurrentExposureAccountCurrency netCurrentExposureAssetCurrency
netInitialExposureAccountCurrency netOvernightFeesAndDividendsAcctCcy netUnits pnlAssetCurrency
repayableLoanAmount totalFees totalFeesAcctCcy totalMarginAccountCurrency
totalMarginAssetCurrency totalTaxes totalTaxesAcctCcy"""
_BREAKDOWN_POSITION_NUMERIC = """acctCcyReturn acctCcyRoePercent addedFunds addedFundsAcctCcy
contracts currentConversionRate currentExposure currentExposureAcctCcy currentRate
fxValueImpact instrumentId leverage liquidationValue liquidationValueAcctCcy margin
marginAcctCcy openConversionRate openRate overnightFeesAndDividends pnl pnlPercent positionId
roePercent totalFeesAcctCcy totalTaxesAcctCcy units"""
_BREAKDOWN_POSITION_TEXT = "assetCurrency direction redeemEligibility settlementType symbol"
_EQUITY_NUMERIC = """available frozenCash currentPNL totalUsedMargin cryptoId balance
totalBalance spendableBalance balanceInFiat totalBalanceInFiat spendableBalanceInFiat
realizedPNL invested positionsValue openOrdersValue openPositionsCount orderIndex"""
_SNAPSHOT_NUMERIC = """totalCash totalInvestedAmount totalPnl totalBalance displayTotalCash
displayTotalInvestedAmount displayTotalPnl displayTotalBalance totalExchangeRate"""
_ACCOUNT_SNAPSHOT_NUMERIC = """cash investedAmount pnl total usdRate displayCash
displayInvestedAmount displayPnl displayTotal exchangeRate"""
_TRADE_NUMERIC = """netProfit closeRate positionId instrumentId leverage openRate stopLossRate
takeProfitRate orderId socialTradeId parentPositionId investment initialInvestment fees units"""
_EXECUTION_NUMERIC = """positionId investedAmountCurrency initialExposureAccountCurrency
initialExposureAssetCurrency addedFunds marginAccountCurrency marginAssetCurrency
remainingUnits remainingContracts stopLossRate takeProfitRate"""
_OPENING_NUMERIC = """orderId units contracts avgPrice avgConversionRate marketSpread markup
priceId fees taxes"""

# The statement's columns, as etoro/fields.py types them (STATEMENT_KINDS).
_ACTIVITY_NUMERIC = """Amount|Units / Contracts|Realized Equity Change|Realized Equity|Balance|
Position ID|NWA"""
_ACTIVITY_TEXT = "Type|Details|Asset type"
_CLOSED_NUMERIC = """Position ID|Amount|Units / Contracts|Leverage|Spread Fees (USD)|
Market Spread (USD)|Profit(USD)|Profit(GBP)|Open Rate|Close Rate|Take profit rate|
Stop loss rate|Overnight Fees and Dividends"""
_CLOSED_TEXT = """Action|Long / Short|FX rate at open (USD)|FX rate at close (USD)|Copied From|
Type|ISIN|Notes"""
_DIVIDEND_NUMERIC = """Net Dividend Received (USD)|Net dividends|Franking Credits (AUD)|
Net Dividend Received (GBP)|Withholding Tax Rate (%)|Withholding Tax Amount (USD)|
Withholding Tax Amount (GBP)|Position ID"""
_DIVIDEND_TEXT = "Instrument Name|Currency|Franked/Unfranked|Type|ISIN"
_SUMMARY_TEXT = "Name|Username|Currency|Account Summary (USD)"
_SUMMARY_TIMES = "Date Created|Start Date|End Date"
_SUMMARY_NUMERIC = """Beginning Realized Equity|Deposits|Internal Transfer|
Transfer to Trading (from eToro Money)|Refunds|Credits|Adjustments|
Profit or Loss (Closed positions only)|Dividends|Dividend CFD|Stocks Lending|Transfer in|
Transfer out|Overnight Fees|Opening and Closing Spread|Admin Fees|SDRT Charge|Withdrawals|
Transfer to eToro Money (from Trading)|Withdrawal Fees|Deposit/Withdrawal FX Conversion Fee|
Ending Realized Equity|Beginning Unrealized Equity|Ending Unrealized Equity"""
_SUMMARY_LABELS = f"{_SUMMARY_TEXT}|{_SUMMARY_TIMES}|{_SUMMARY_NUMERIC}"

_P = "clientPortfolio.positions[]."
_A = "instrumentAggregates[]."
_BI = "instruments[]."
_BP = "instruments[].positions[]."
_BAL = "balances[]."
_S = "snapshots[]."
_AS = "snapshots[].accountSnapshots[]."
_BANK = "bankTransferTransactionDetails.bankIdentifier[]."
_EX = "positionExecutions[]."
_IMG = "images[]."

_UPGRADE_TABLES = [
    # /me, the connection's user: one row, the latest.
    _table(
        "etoro_users",
        [_CONNECTION, _RAW],
        _columns("numeric", "gcid realCid demoCid playerLevel gender language", "gcid realCid"),
        _columns("text", "username firstName middleName lastName dateOfBirth avatarUrl"),
        tail=["PRIMARY KEY (connection_id)"],
    ),
    _table(
        "etoro_user_scopes",
        [_CONNECTION, _INDEX, _RAW],
        ["scope text NOT NULL"],
        tail=[
            "PRIMARY KEY (connection_id, item_index)",
            _child_of("etoro_users", "connection_id", "connection_id"),
        ],
    ),
    # PnL, one row per sync day, and its positions.
    _table(
        "etoro_pnl",
        [_CONNECTION, _DAY, _RAW],
        _columns(
            "numeric",
            """clientPortfolio.accountCurrencyId clientPortfolio.bonusCredit
            clientPortfolio.credit clientPortfolio.unrealizedPnL""",
            "clientPortfolio.credit",
        ),
        tail=["PRIMARY KEY (connection_id, as_of_date)"],
    ),
    _table(
        "etoro_pnl_positions",
        [_CONNECTION, _DAY, _INDEX, _INSTRUMENT, _RAW],
        _columns(
            "numeric",
            _under(_P, _PNL_POSITION_NUMERIC),
            f"{_P}positionID {_P}instrumentID",
            prefix=_P,
        ),
        _columns("timestamptz", f"{_P}openDateTime {_P}unrealizedPnL.timestamp", prefix=_P),
        _columns("boolean", _under(_P, _PNL_POSITION_BOOLEAN), prefix=_P),
        tail=[
            "PRIMARY KEY (connection_id, as_of_date, item_index)",
            _child_of("etoro_pnl", "connection_id, as_of_date", "connection_id, as_of_date"),
        ],
    ),
    # aggregate-portfolio, per sync day; each instrument links its reported holding.
    _table(
        "etoro_aggregates",
        [_CONNECTION, _DAY, _RAW],
        _columns(
            "numeric",
            f"cid {_under('accountTotals.', _ACCOUNT_TOTALS)}",
            "accountTotals.accountTotalValue",
        ),
        _columns("text", "accountCurrency tradingFundingType", "accountCurrency"),
        _columns("timestamptz", "timestamp"),
        tail=["PRIMARY KEY (connection_id, as_of_date)"],
    ),
    _table(
        "etoro_aggregate_instruments",
        [_CONNECTION, _DAY, _INDEX, _INSTRUMENT, _SNAPSHOT, _RAW],
        _columns("numeric", _under(_A, _AGGREGATE_NUMERIC), f"{_A}instrumentId", prefix=_A),
        _columns("text", f"{_A}assetCurrency {_A}loanCcy", prefix=_A),
        tail=[
            "PRIMARY KEY (connection_id, as_of_date, item_index)",
            _child_of("etoro_aggregates", "connection_id, as_of_date", "connection_id, as_of_date"),
        ],
    ),
    # instrument-breakdown, per sync day: instruments and their positions.
    _table(
        "etoro_breakdowns",
        [_CONNECTION, _DAY, _RAW],
        _columns("text", "accountCurrency", "accountCurrency"),
        _columns("timestamptz", "timestamp"),
        tail=["PRIMARY KEY (connection_id, as_of_date)"],
    ),
    _table(
        "etoro_breakdown_instruments",
        [_CONNECTION, _DAY, _INDEX, _INSTRUMENT, _RAW],
        _columns("numeric", f"{_BI}instrumentId", f"{_BI}instrumentId", prefix=_BI),
        _columns("text", f"{_BI}symbol", prefix=_BI),
        tail=[
            "PRIMARY KEY (connection_id, as_of_date, item_index)",
            _child_of("etoro_breakdowns", "connection_id, as_of_date", "connection_id, as_of_date"),
        ],
    ),
    _table(
        "etoro_breakdown_positions",
        [
            _CONNECTION,
            _DAY,
            "instrument_index integer NOT NULL",
            _INDEX,
            _INSTRUMENT,
            _RAW,
        ],
        _columns(
            "numeric",
            _under(_BP, _BREAKDOWN_POSITION_NUMERIC),
            f"{_BP}positionId {_BP}units",
            prefix=_BP,
        ),
        _columns("text", _under(_BP, _BREAKDOWN_POSITION_TEXT), prefix=_BP),
        _columns("timestamp", f"{_BP}openTime {_BP}rateTimestamp", prefix=_BP),
        _columns("boolean", f"{_BP}isSettled {_BP}repayLoanEligibility", prefix=_BP),
        tail=[
            "PRIMARY KEY (connection_id, as_of_date, instrument_index, item_index)",
            _child_of(
                "etoro_breakdown_instruments",
                "connection_id, as_of_date, instrument_index",
                "connection_id, as_of_date, item_index",
            ),
        ],
    ),
    # balances, per sync day: every account; each links its account and reported cash.
    _table(
        "etoro_balances",
        [_CONNECTION, _DAY, _RAW],
        _columns("numeric", "gcid totalBalance"),
        _columns("text", "displayCurrency"),
        tail=["PRIMARY KEY (connection_id, as_of_date)"],
    ),
    _table(
        "etoro_balance_accounts",
        [_CONNECTION, _DAY, _INDEX, _ACCOUNT, _SNAPSHOT, _RAW],
        _columns(
            "numeric",
            f"""{_under(_BAL, "balance displayBalance exchangeRate")}
            {_under(f"{_BAL}equityDetails.", _EQUITY_NUMERIC)}""",
            f"{_BAL}balance",
            prefix=_BAL,
        ),
        _columns(
            "text",
            f"""{_under(_BAL, "accountId accountType subType currency displayCurrency")}
            {_BAL}equityDetails.fiatConversionCurrency""",
            f"{_BAL}accountId {_BAL}accountType {_BAL}currency",
            prefix=_BAL,
        ),
        _columns("boolean", f"{_BAL}equityDetails.hasOpenPositions", prefix=_BAL),
        tail=[
            "PRIMARY KEY (connection_id, as_of_date, item_index)",
            _child_of("etoro_balances", "connection_id, as_of_date", "connection_id, as_of_date"),
        ],
    ),
    # balances/history: each request, each day's snapshot (the latest request's), and each
    # account's line of it, linked to its account and reported snapshot.
    _table(
        "etoro_balance_history",
        [_CONNECTION, _RAW],
        _columns("numeric", "gcid"),
        _columns("text", "displayCurrency"),
        _columns("date", "fromDate toDate"),
        tail=["PRIMARY KEY (connection_id, raw_record_id)"],
    ),
    _table(
        "etoro_balance_snapshots",
        [_CONNECTION, _RAW],
        _columns("date", f"{_S}date", f"{_S}date", prefix=_S),
        _columns("numeric", _under(_S, _SNAPSHOT_NUMERIC), prefix=_S),
        _columns("text", f"{_S}totalCurrencyIso", prefix=_S),
        tail=["PRIMARY KEY (connection_id, date)"],
    ),
    _table(
        "etoro_balance_account_snapshots",
        [_CONNECTION, "snapshot_date date NOT NULL", _INDEX, _ACCOUNT, _SNAPSHOT, _RAW],
        _columns("numeric", _under(_AS, _ACCOUNT_SNAPSHOT_NUMERIC), f"{_AS}total", prefix=_AS),
        _columns(
            "text",
            _under(_AS, "accountId accountType currency"),
            _under(_AS, "accountId accountType currency"),
            prefix=_AS,
        ),
        tail=[
            "PRIMARY KEY (connection_id, snapshot_date, item_index)",
            _child_of(
                "etoro_balance_snapshots", "connection_id, snapshot_date", "connection_id, date"
            ),
        ],
    ),
    # eToro Money: `cash:<id>` (Decision 9).
    _table(
        "etoro_cash_transactions",
        [_CONNECTION, _LEDGER, _ACCOUNT, _RAW],
        _columns(
            "text",
            f"""id accountId transactionType transactionSubtype direction status currency
            originalCurrency counterparty.name counterparty.type
            {_under("cardTransactionDetails.", "cardId merchantName country authorizationStatus")}
            {_under("bankTransferTransactionDetails.", "description paymentReference")}
            internalTransferTransactionDetails.transferId""",
            "id accountId transactionType direction status currency",
        ),
        _columns("numeric", "amount originalAmount conversionRate", "amount"),
        _columns("timestamptz", "postedAt", "postedAt"),
        tail=["PRIMARY KEY (connection_id, id)"],
    ),
    _table(
        "etoro_cash_bank_identifiers",
        [_CONNECTION, "cash_transaction_id text NOT NULL", _INDEX, _RAW],
        _columns("text", _under(_BANK, "name value"), prefix=_BANK),
        tail=[
            "PRIMARY KEY (connection_id, cash_transaction_id, item_index)",
            _child_of(
                "etoro_cash_transactions", "connection_id, cash_transaction_id", "connection_id, id"
            ),
        ],
    ),
    # A close, `close:<positionId>`: trade history's fields and Closed Positions' columns,
    # from either source or both, so neither's are NOT NULL.
    _table(
        "etoro_trades",
        [
            _CONNECTION,
            "external_id text NOT NULL CHECK (external_id ~ '^close:[1-9][0-9]*$')",
            _LEDGER,
            _INSTRUMENT,
            "raw_record_id uuid REFERENCES core.raw_records (id)",
            "statement_raw_record_id uuid REFERENCES core.raw_records (id)",
            "statement_row_key text CHECK (statement_row_key ~ '^[0-9a-f]{32}$')",
        ],
        _columns("numeric", _TRADE_NUMERIC),
        _columns("timestamptz", "openTimestamp closeTimestamp"),
        _columns("boolean", "isBuy trailingStopLoss"),
        _headers("numeric", _CLOSED_NUMERIC, prefix="statement_"),
        _headers("timestamptz", "Open Date|Close Date", prefix="statement_"),
        _headers("text", _CLOSED_TEXT, prefix="statement_"),
        tail=[
            "PRIMARY KEY (connection_id, external_id)",
            "CHECK (raw_record_id IS NOT NULL OR statement_raw_record_id IS NOT NULL)",
            "CHECK ((statement_raw_record_id IS NULL) = (statement_row_key IS NULL))",
        ],
    ),
    # orders:lookup, each order once; an execution is the open of its position.
    _table(
        "etoro_orders",
        [_CONNECTION, _INSTRUMENT, _RAW],
        _columns(
            "numeric",
            """accountId gcid portfolioId orderId etoroOrderTypeId status.id status.errorCode
            asset.instrumentId asset.leverage requestedAmount requestedUnits
            requestedContracts frozenAmount requestedTriggerRate openStopLossRate
            openTakeProfitRate totalCosts""",
            "orderId",
        ),
        _columns(
            "text",
            f"""action transaction type status.name status.errorMessage
            {_under("asset.", "symbol currency settlementType side")} orderCurrency
            stopLossType openActionType requestType""",
        ),
        _columns("timestamptz", "requestTime lastUpdate"),
        tail=["PRIMARY KEY (connection_id, order_id)"],
    ),
    _table(
        "etoro_position_executions",
        [_CONNECTION, "parent_order_id numeric NOT NULL", _INDEX, _LEDGER, _RAW],
        _columns(
            "numeric",
            f"""{_under(_EX, _EXECUTION_NUMERIC)}
            {_under(f"{_EX}openingData.", _OPENING_NUMERIC)}""",
            f"{_EX}positionId",
            prefix=_EX,
        ),
        _columns("text", f"{_EX}state", prefix=_EX),
        _columns(
            "timestamptz",
            f"{_EX}openingData.openTime {_EX}openingData.executionTime",
            prefix=_EX,
        ),
        tail=[
            "PRIMARY KEY (connection_id, parent_order_id, item_index)",
            _child_of("etoro_orders", "connection_id, parent_order_id", "connection_id, order_id"),
        ],
    ),
    _table(
        "etoro_order_positions_to_close",
        [_CONNECTION, "parent_order_id numeric NOT NULL", _INDEX, _RAW],
        [_line("position_id", "numeric", True)],
        tail=[
            "PRIMARY KEY (connection_id, parent_order_id, item_index)",
            _child_of("etoro_orders", "connection_id, parent_order_id", "connection_id, order_id"),
        ],
    ),
    _table(
        "etoro_closed_events",
        [_CONNECTION, _RAW],
        _columns("numeric", "closeYear closedPositionEvents", "closeYear closedPositionEvents"),
        _columns("text", "assetType", "assetType"),
        tail=["PRIMARY KEY (connection_id, close_year, asset_type)"],
    ),
    _table(
        "etoro_stockback_payments",
        [_CONNECTION, _RAW],
        _columns("numeric", "paidUsd positionId"),
        _columns("text", "channel stockName sourceTxId txLabel"),
        _columns("date", "paymentDate eom"),
        tail=["PRIMARY KEY (connection_id, raw_record_id)"],
    ),
    # Market data.
    _table(
        "etoro_instruments",
        [_CONNECTION, _INSTRUMENT, _RAW],
        _columns(
            "numeric",
            "instrumentID instrumentTypeID exchangeID stocksIndustryID distributionType",
            "instrumentID",
        ),
        _columns("text", "instrumentDisplayName symbolFull priceSource"),
        _columns("boolean", "hasExpirationDate isInternalInstrument"),
        tail=["PRIMARY KEY (connection_id, instrument_id)"],
    ),
    _table(
        "etoro_instrument_images",
        [_CONNECTION, "parent_instrument_id numeric NOT NULL", _INDEX, _RAW],
        _columns("numeric", _under(_IMG, "instrumentID width height"), prefix=_IMG),
        _columns("text", _under(_IMG, "uri backgroundColor textColor"), prefix=_IMG),
        tail=[
            "PRIMARY KEY (connection_id, parent_instrument_id, item_index)",
            _child_of(
                "etoro_instruments",
                "connection_id, parent_instrument_id",
                "connection_id, instrument_id",
            ),
        ],
    ),
    _table(
        "etoro_instrument_types",
        [_CONNECTION, _RAW],
        _columns("numeric", "instrumentTypeID", "instrumentTypeID"),
        _columns("text", "instrumentTypeDescription"),
        tail=["PRIMARY KEY (connection_id, instrument_type_id)"],
    ),
    _table(
        "etoro_exchanges",
        [_CONNECTION, _RAW],
        _columns("numeric", "exchangeID", "exchangeID"),
        _columns("text", "exchangeDescription"),
        tail=["PRIMARY KEY (connection_id, exchange_id)"],
    ),
    _table(
        "etoro_candles",
        [_CONNECTION, _RAW],
        _columns("numeric", "instrumentId open high low close volume", "instrumentId"),
        _columns("timestamptz", "time", "time"),
        tail=["PRIMARY KEY (connection_id, instrument_id, time)"],
    ),
    # The statement: one row per document (Account Summary's labels, column C in `_2`),
    # then a row per Account Activity, Dividends and Financial Summary row by its key.
    _table(
        "etoro_statements",
        [_CONNECTION, "document_id uuid NOT NULL REFERENCES core.documents (id)"],
        _headers("text", _SUMMARY_TEXT),
        _headers("timestamptz", _SUMMARY_TIMES),
        _headers("numeric", _SUMMARY_NUMERIC),
        _headers("text", _SUMMARY_LABELS, suffix="_2"),
        tail=["PRIMARY KEY (connection_id, document_id)"],
    ),
    _table(
        "etoro_activity",
        [_CONNECTION, _ROW_KEY, _ENRICHES, _RAW],
        _headers("timestamptz", "Date"),
        _headers("numeric", _ACTIVITY_NUMERIC),
        _headers("text", _ACTIVITY_TEXT),
        tail=["PRIMARY KEY (connection_id, row_key)"],
    ),
    _table(
        "etoro_dividends",
        [_CONNECTION, _ROW_KEY, _ENRICHES, _INSTRUMENT, _RAW],
        _headers("date", "Date of Payment"),
        _headers("numeric", _DIVIDEND_NUMERIC),
        _headers("text", _DIVIDEND_TEXT),
        tail=["PRIMARY KEY (connection_id, row_key)"],
    ),
    _table(
        "etoro_financial_summary",
        [
            _CONNECTION,
            _ROW_KEY,
            "period_from timestamptz NOT NULL",
            "period_to timestamptz NOT NULL",
            _RAW,
        ],
        ["name text NOT NULL"],
        tail=["PRIMARY KEY (connection_id, row_key)"],
    ),
    # Financial Summary's columns after Name are its periods, which change per statement.
    _table(
        "etoro_financial_summary_values",
        [
            _CONNECTION,
            _ROW_KEY,
            _INDEX,
            "period_header text NOT NULL",
            _line("figure", "numeric", False),
            _RAW,
        ],
        tail=[
            "PRIMARY KEY (connection_id, row_key, item_index)",
            _child_of(
                "etoro_financial_summary", "connection_id, row_key", "connection_id, row_key"
            ),
        ],
    ),
]

_TABLES = [
    "etoro_users",
    "etoro_user_scopes",
    "etoro_pnl",
    "etoro_pnl_positions",
    "etoro_aggregates",
    "etoro_aggregate_instruments",
    "etoro_breakdowns",
    "etoro_breakdown_instruments",
    "etoro_breakdown_positions",
    "etoro_balances",
    "etoro_balance_accounts",
    "etoro_balance_history",
    "etoro_balance_snapshots",
    "etoro_balance_account_snapshots",
    "etoro_cash_transactions",
    "etoro_cash_bank_identifiers",
    "etoro_trades",
    "etoro_orders",
    "etoro_position_executions",
    "etoro_order_positions_to_close",
    "etoro_closed_events",
    "etoro_stockback_payments",
    "etoro_instruments",
    "etoro_instrument_images",
    "etoro_instrument_types",
    "etoro_exchanges",
    "etoro_candles",
    "etoro_statements",
    "etoro_activity",
    "etoro_dividends",
    "etoro_financial_summary",
    "etoro_financial_summary_values",
]

_SOURCES = [
    # A value two sources of one event disagree on (design §5.7, Decision 14); at most one
    # open per event and field. Resolving it records the owner's decision as well.
    """
    CREATE TABLE finance.source_reviews (
      id uuid PRIMARY KEY DEFAULT uuidv7(),
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      transaction_id uuid REFERENCES finance.transactions (id) ON DELETE SET NULL,
      event_key text NOT NULL CHECK (event_key <> ''),
      field text NOT NULL CHECK (field <> ''),
      api_value text NOT NULL,
      statement_value text NOT NULL,
      opened_at timestamptz NOT NULL DEFAULT now(),
      resolved_at timestamptz,
      resolution text CHECK (resolution IN ('api', 'statement')),
      resolved_by text,
      CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
      CHECK ((resolved_at IS NULL) = (resolved_by IS NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX source_reviews_one_open
      ON finance.source_reviews (connection_id, event_key, field) WHERE resolved_at IS NULL
    """,
    "CREATE INDEX source_reviews_connection ON finance.source_reviews (connection_id, opened_at)",
    # The owner's take on a field of an event, which the normalizer replays.
    """
    CREATE TABLE finance.source_decisions (
      connection_id uuid NOT NULL REFERENCES core.connections (id),
      event_key text NOT NULL CHECK (event_key <> ''),
      field text NOT NULL CHECK (field <> ''),
      take text NOT NULL CHECK (take IN ('api', 'statement')),
      decided_at timestamptz NOT NULL DEFAULT now(),
      decided_by text NOT NULL,
      PRIMARY KEY (connection_id, event_key, field)
    )
    """,
    # A transfer leg's conversion fee (design §5.12, Decision 15): written by its connector,
    # read by the transfer engine's settlement.
    f"""
    CREATE TABLE finance.transfer_leg_fees (
      transaction_id uuid PRIMARY KEY REFERENCES finance.transactions (id) ON DELETE CASCADE,
      instrument_id uuid NOT NULL REFERENCES finance.instruments (id),
      amount numeric NOT NULL CHECK (amount NOT IN {_NON_FINITE} AND amount >= 0)
    )
    """,
]

# An eToro account id: a Cash (eToro Money) account's is a UUID, stored in lower case; a
# Trading or MoneyFarm account's is digits, as eToro writes it.
_ETORO_ACCOUNT = "'^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9]{1,19})$'"

UPGRADE = [
    f"""
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account', 'trading212_account',
          'etoro_account')),
      ADD CONSTRAINT account_identifiers_etoro_account_check
        CHECK (kind <> 'etoro_account' OR value ~ {_ETORO_ACCOUNT})
    """,
    *_UPGRADE_TABLES,
    *_SOURCES,
]

DOWNGRADE = [
    "DROP TABLE finance.transfer_leg_fees",
    "DROP TABLE finance.source_decisions",
    "DROP TABLE finance.source_reviews",
    *(f"DROP TABLE finance.{name}" for name in reversed(_TABLES)),
    "DELETE FROM finance.account_identifiers WHERE kind = 'etoro_account'",
    """
    ALTER TABLE finance.account_identifiers
      DROP CONSTRAINT account_identifiers_etoro_account_check,
      DROP CONSTRAINT account_identifiers_kind_check,
      ADD CONSTRAINT account_identifiers_kind_check
        CHECK (kind IN ('uk_bank_account', 'ibkr_account', 'trading212_account'))
    """,
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
