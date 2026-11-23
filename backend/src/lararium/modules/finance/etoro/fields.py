"""eToro's endpoints and fields: the single source for the sync, the normalizer and the typed
tables (Decision 3).

Known paths (Decision 4) are every path the owner's read-key probe showed (2026-11-23), plus
the SPEC fields (eToro's OpenAPI v1.419.0) of trade history, `orders:lookup`, balances, balance
history, cash transactions, closed-events, stockback and the instrument metadata. Where SPEC
and the probe spell one field differently (`stocksIndustryId`, the probe's
`stocksIndustryID`), the probe's spelling, which is what the API sent, is the known path.

A path is dotted, below one item: the object an endpoint returns, or one item of the list the
client returns (trade history rows, cash transactions, `results` items, instruments). An array
of objects is a child table whose items' paths are `<array>[].<field>`; an array inside an item
nests. An array of plain values (`scopes[]`) is a child table of one column, `SCALAR_ITEMS`.

A required path must be present and not null in every item; one under `<array>[]` in every item
of that array, when the array is there. Any other path with a value is flagged by name.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from lararium.modules.finance.typed_tables import column_map


@dataclass(frozen=True)
class EndpointSpec:
    record_type: str
    table: str
    required: frozenset[str]
    known: frozenset[str]
    # An array's (`…[]`) path to the child table its items are rows of.
    children: Mapping[str, str]


def _f(names: str) -> frozenset[str]:
    return frozenset(names.split())


def _under(prefix: str, names: str) -> str:
    return " ".join(f"{prefix}{name}" for name in names.split())


# `timestamp` is a zone-less date-time (Decision 3), as instrument-breakdown's `openTime`.
type Kind = Literal["numeric", "text", "timestamptz", "timestamp", "date", "boolean"]

_ME_SCOPES = "scopes[]"
_PNL_POSITIONS = "clientPortfolio.positions[]"
_AGGREGATES = "instrumentAggregates[]"
_BREAKDOWN_INSTRUMENTS = "instruments[]"
_BREAKDOWN_POSITIONS = "instruments[].positions[]"
_BALANCES = "balances[]"
_SNAPSHOTS = "snapshots[]"
_ACCOUNT_SNAPSHOTS = "snapshots[].accountSnapshots[]"
_BANK_IDENTIFIERS = "bankTransferTransactionDetails.bankIdentifier[]"
_EXECUTIONS = "positionExecutions[]"
_POSITIONS_TO_CLOSE = "positionsToClose[]"
_IMAGES = "images[]"

# An array of plain values: its child table's one column.
SCALAR_ITEMS: Mapping[str, str] = {_ME_SCOPES: "scope", _POSITIONS_TO_CLOSE: "position_id"}

# Probe: the PnL position and its unrealised PnL.
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
# SPEC (the probe showed the Trading, Cash and MoneyFarm ones).
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


def _endpoint(
    record_type: str,
    table: str,
    required: str,
    *,
    numeric: str = "",
    text: str = "",
    timestamptz: str = "",
    timestamp: str = "",
    date: str = "",
    boolean: str = "",
    children: Mapping[str, str] | None = None,
) -> tuple[EndpointSpec, dict[str, Kind]]:
    """An endpoint's spec and each known path's kind; the known paths are those given a kind."""
    declared: dict[Kind, str] = {
        "numeric": numeric,
        "text": text,
        "timestamptz": timestamptz,
        "timestamp": timestamp,
        "date": date,
        "boolean": boolean,
    }
    kinds: dict[str, Kind] = {}
    for kind, paths in declared.items():
        for path in paths.split():
            if kinds.setdefault(path, kind) != kind:
                raise ValueError(f"{record_type} declares {path} twice")
    spec = EndpointSpec(record_type, table, _f(required), frozenset(kinds), children or {})
    return spec, kinds


_DECLARED: Mapping[str, tuple[EndpointSpec, dict[str, Kind]]] = {
    # Probe and SPEC agree. Personal fields are stored here, never logged (PERSONAL).
    "me": _endpoint(
        "etoro.me",
        "etoro_users",
        "gcid realCid",
        numeric="gcid realCid demoCid playerLevel gender language",
        # `dateOfBirth`: SPEC gives no format, so it is kept as sent.
        text=f"username firstName middleName lastName dateOfBirth avatarUrl {_ME_SCOPES}",
        children={_ME_SCOPES: "etoro_user_scopes"},
    ),
    # Probe only (not a SPEC endpoint of Decision 4).
    "pnl": _endpoint(
        "etoro.pnl",
        "etoro_pnl",
        f"clientPortfolio.credit {_PNL_POSITIONS}.positionID {_PNL_POSITIONS}.instrumentID",
        numeric=f"""{_under("clientPortfolio.", "accountCurrencyId bonusCredit credit")}
        clientPortfolio.unrealizedPnL {_under(f"{_PNL_POSITIONS}.", _PNL_POSITION_NUMERIC)}""",
        timestamptz=f"{_PNL_POSITIONS}.openDateTime {_PNL_POSITIONS}.unrealizedPnL.timestamp",
        boolean=_under(f"{_PNL_POSITIONS}.", _PNL_POSITION_BOOLEAN),
        children={_PNL_POSITIONS: "etoro_pnl_positions"},
    ),
    # Probe only.
    "aggregate": _endpoint(
        "etoro.aggregate",
        "etoro_aggregates",
        f"accountCurrency accountTotals.accountTotalValue {_AGGREGATES}.instrumentId",
        numeric=f"""cid {_under("accountTotals.", _ACCOUNT_TOTALS)}
        {_under(f"{_AGGREGATES}.", _AGGREGATE_NUMERIC)}""",
        text=f"""accountCurrency tradingFundingType {_AGGREGATES}.assetCurrency
        {_AGGREGATES}.loanCcy""",
        timestamptz="timestamp",
        children={_AGGREGATES: "etoro_aggregate_instruments"},
    ),
    # Probe only. `openTime` and `rateTimestamp` carry no zone.
    "breakdown": _endpoint(
        "etoro.breakdown",
        "etoro_breakdowns",
        f"""accountCurrency {_BREAKDOWN_INSTRUMENTS}.instrumentId
        {_BREAKDOWN_POSITIONS}.positionId {_BREAKDOWN_POSITIONS}.units""",
        numeric=f"""{_BREAKDOWN_INSTRUMENTS}.instrumentId
        {_under(f"{_BREAKDOWN_POSITIONS}.", _BREAKDOWN_POSITION_NUMERIC)}""",
        text=f"""accountCurrency {_BREAKDOWN_INSTRUMENTS}.symbol
        {_under(f"{_BREAKDOWN_POSITIONS}.", _BREAKDOWN_POSITION_TEXT)}""",
        timestamptz="timestamp",
        timestamp=f"{_BREAKDOWN_POSITIONS}.openTime {_BREAKDOWN_POSITIONS}.rateTimestamp",
        boolean=f"{_BREAKDOWN_POSITIONS}.isSettled {_BREAKDOWN_POSITIONS}.repayLoanEligibility",
        children={
            _BREAKDOWN_INSTRUMENTS: "etoro_breakdown_instruments",
            _BREAKDOWN_POSITIONS: "etoro_breakdown_positions",
        },
    ),
    "balances": _endpoint(
        "etoro.balances",
        "etoro_balances",
        f"{_BALANCES}.accountId {_BALANCES}.accountType {_BALANCES}.balance {_BALANCES}.currency",
        numeric=f"""gcid totalBalance
        {_under(f"{_BALANCES}.", "balance displayBalance exchangeRate")}
        {_under(f"{_BALANCES}.equityDetails.", _EQUITY_NUMERIC)}""",
        text=f"""displayCurrency
        {_under(f"{_BALANCES}.", "accountId accountType subType currency displayCurrency")}
        {_BALANCES}.equityDetails.fiatConversionCurrency""",
        boolean=f"{_BALANCES}.equityDetails.hasOpenPositions",
        children={_BALANCES: "etoro_balance_accounts"},
    ),
    "balance_history": _endpoint(
        "etoro.balance_history",
        "etoro_balance_history",
        f"""{_SNAPSHOTS}.date {_ACCOUNT_SNAPSHOTS}.accountId {_ACCOUNT_SNAPSHOTS}.accountType
        {_ACCOUNT_SNAPSHOTS}.currency {_ACCOUNT_SNAPSHOTS}.total""",
        numeric=f"""gcid {_under(f"{_SNAPSHOTS}.", _SNAPSHOT_NUMERIC)}
        {_under(f"{_ACCOUNT_SNAPSHOTS}.", _ACCOUNT_SNAPSHOT_NUMERIC)}""",
        text=f"""displayCurrency {_SNAPSHOTS}.totalCurrencyIso
        {_under(f"{_ACCOUNT_SNAPSHOTS}.", "accountId accountType currency")}""",
        date=f"fromDate toDate {_SNAPSHOTS}.date",
        children={
            _SNAPSHOTS: "etoro_balance_snapshots",
            _ACCOUNT_SNAPSHOTS: "etoro_balance_account_snapshots",
        },
    ),
    # One `results` item. eToro Money amounts are decimal strings.
    "cash_transactions": _endpoint(
        "etoro.cash_transaction",
        "etoro_cash_transactions",
        "id accountId transactionType direction status amount currency postedAt",
        numeric="amount originalAmount conversionRate",
        text=f"""id accountId transactionType transactionSubtype direction status currency
        originalCurrency counterparty.name counterparty.type
        {_under("cardTransactionDetails.", "cardId merchantName country authorizationStatus")}
        {_under(f"{_BANK_IDENTIFIERS}.", "name value")}
        {_under("bankTransferTransactionDetails.", "description paymentReference")}
        internalTransferTransactionDetails.transferId""",
        timestamptz="postedAt",
        children={_BANK_IDENTIFIERS: "etoro_cash_bank_identifiers"},
    ),
    # One row of the bare array.
    "trade_history": _endpoint(
        "etoro.trade",
        "etoro_trades",
        "positionId instrumentId openTimestamp closeTimestamp units",
        numeric=_TRADE_NUMERIC,
        timestamptz="openTimestamp closeTimestamp",
        boolean="isBuy trailingStopLoss",
    ),
    "order_lookup": _endpoint(
        "etoro.order",
        "etoro_orders",
        f"orderId {_EXECUTIONS}.positionId",
        numeric=f"""accountId gcid portfolioId orderId etoroOrderTypeId status.id status.errorCode
        asset.instrumentId asset.leverage requestedAmount requestedUnits requestedContracts
        frozenAmount requestedTriggerRate openStopLossRate openTakeProfitRate totalCosts
        {_POSITIONS_TO_CLOSE} {_under(f"{_EXECUTIONS}.", _EXECUTION_NUMERIC)}
        {_under(f"{_EXECUTIONS}.openingData.", _OPENING_NUMERIC)}""",
        text=f"""action transaction type status.name status.errorMessage
        {_under("asset.", "symbol currency settlementType side")} orderCurrency stopLossType
        openActionType requestType {_EXECUTIONS}.state""",
        timestamptz=f"""requestTime lastUpdate {_EXECUTIONS}.openingData.openTime
        {_EXECUTIONS}.openingData.executionTime""",
        children={
            _EXECUTIONS: "etoro_position_executions",
            _POSITIONS_TO_CLOSE: "etoro_order_positions_to_close",
        },
    ),
    # One `results` item.
    "closed_events": _endpoint(
        "etoro.closed_events",
        "etoro_closed_events",
        "closeYear assetType closedPositionEvents",
        numeric="closeYear closedPositionEvents",
        text="assetType",
    ),
    # One `results` item (SPEC; the probe's list was empty).
    "stockback": _endpoint(
        "etoro.stockback",
        "etoro_stockback_payments",
        "",
        numeric="paidUsd positionId",
        text="channel stockName sourceTxId txLabel",
        date="paymentDate eom",
    ),
    # One `instrumentDisplayDatas` item: probe and SPEC (the probe's `stocksIndustryID`).
    "instruments": _endpoint(
        "etoro.instrument",
        "etoro_instruments",
        "instrumentID",
        numeric=f"""instrumentID instrumentTypeID exchangeID stocksIndustryID distributionType
        {_under(f"{_IMAGES}.", "instrumentID width height")}""",
        text=f"""instrumentDisplayName symbolFull priceSource
        {_under(f"{_IMAGES}.", "uri backgroundColor textColor")}""",
        boolean="hasExpirationDate isInternalInstrument",
        children={_IMAGES: "etoro_instrument_images"},
    ),
    # One `instrumentTypes` item (SPEC).
    "instrument_types": _endpoint(
        "etoro.instrument_type",
        "etoro_instrument_types",
        "instrumentTypeID",
        numeric="instrumentTypeID",
        text="instrumentTypeDescription",
    ),
    # One `exchangeInfo` item (SPEC).
    "exchanges": _endpoint(
        "etoro.exchange",
        "etoro_exchanges",
        "exchangeID",
        numeric="exchangeID",
        text="exchangeDescription",
    ),
    # One daily candle of `results` (SPEC); its prices are decimal strings.
    "candles": _endpoint(
        "etoro.candle",
        "etoro_candles",
        "time",
        numeric="open high low close volume",
        timestamptz="time",
    ),
}

ENDPOINTS: Mapping[str, EndpointSpec] = {name: spec for name, (spec, _) in _DECLARED.items()}
# Endpoint → known path → the type of its column (Decision 3).
KINDS: Mapping[str, Mapping[str, Kind]] = {name: kinds for name, (_, kinds) in _DECLARED.items()}

# Stored typed, but never put in a log, warning or message: `/me`'s names, date of birth,
# gender, avatar and username, and counterparties' names and bank details.
PERSONAL: frozenset[str] = frozenset(
    {
        "firstName",
        "middleName",
        "lastName",
        "dateOfBirth",
        "gender",
        "avatarUrl",
        "username",
        "counterparty.name",
        f"{_BANK_IDENTIFIERS}.value",
        "bankTransferTransactionDetails.paymentReference",
    }
)

# Columns of the tables that are not a field: references, keys and links. A field whose column
# were one of these would overwrite it. eToro's own `accountId` and `instrumentId` are fields
# (`account_id`, `instrument_id`), so links to finance's accounts and instruments need other
# names.
OWN_COLUMNS = frozenset(
    {
        "connection_id",
        "raw_record_id",
        "as_of_date",
        "transaction_id",
        "external_id",
        "item_index",
    }
)


def split(spec: EndpointSpec, path: str) -> tuple[str, str]:
    """The table a known path's value goes to, and the path below that table's row. An array of
    plain values is its child table's one column (SCALAR_ITEMS); an array of objects is not a
    path of its own."""
    if path in spec.children:
        if path not in SCALAR_ITEMS:
            raise ValueError(f"{spec.record_type}: {path} is an array of objects, not a field")
        return spec.children[path], SCALAR_ITEMS[path]
    owner = max((c for c in spec.children if path.startswith(f"{c}.")), key=len, default=None)
    if owner is None:
        return spec.table, path
    return spec.children[owner], path.removeprefix(f"{owner}.")


def _columns(
    endpoints: Mapping[str, EndpointSpec], personal: frozenset[str]
) -> dict[str, dict[str, dict[str, str]]]:
    """Endpoint → table → known path → column. Built through `column_map`, per table across
    every endpoint writing it, so two paths that would share a column fail at import, as do a
    required or personal path that is not known and a field whose column is an own column."""
    by_table: dict[str, set[str]] = {}
    for spec in endpoints.values():
        unknown = spec.required - spec.known
        if unknown:
            raise ValueError(f"{spec.record_type} requires unknown {sorted(unknown)}")
        for path in spec.known:
            table, below = split(spec, path)
            by_table.setdefault(table, set()).add(below)
    table_columns = {table: column_map(sorted(paths)) for table, paths in by_table.items()}
    for table, mapping in table_columns.items():
        if clash := OWN_COLUMNS & set(mapping.values()):
            raise ValueError(f"{table}: a field would be column {', '.join(sorted(clash))}")
    known = frozenset[str]().union(*(spec.known for spec in endpoints.values()))
    if missing := personal - known:
        raise ValueError(f"personal fields not known: {sorted(missing)}")
    return {name: _endpoint_columns(spec, table_columns) for name, spec in endpoints.items()}


def _endpoint_columns(
    spec: EndpointSpec, table_columns: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for path in sorted(spec.known):
        table, below = split(spec, path)
        result.setdefault(table, {})[path] = table_columns[table][below]
    return result


# Endpoint → table → known path → column (Decision 3: `column_name` of the path below the row).
COLUMNS: Mapping[str, Mapping[str, Mapping[str, str]]] = _columns(ENDPOINTS, PERSONAL)
