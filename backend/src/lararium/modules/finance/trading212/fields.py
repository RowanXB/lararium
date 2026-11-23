"""Trading 212's endpoints and fields: the single source for the sync, the normalizer and the
typed tables of migration 0030.

Known paths are every path of the OpenAPI bundle of 2026-10-17 for the endpoints the
connector reads. A path is dotted, below one item (a page's item, an array's item, or the
object an endpoint returns). An array of objects is a child table: its items' paths are
`<array>[].<field>`, and an array inside an item nests (`workingSchedules[].timeEvents[].type`).
A map is `<map>{}`; each entry is a child row whose two fields `MAP_ENTRIES` names (key, value).

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
    # An array's (`…[]`) or a map's (`…{}`) path to the child table its items are rows of.
    children: Mapping[str, str]


def _f(names: str) -> frozenset[str]:
    return frozenset(names.split())


def _under(prefix: str, names: str) -> str:
    return " ".join(f"{prefix}{name}" for name in names.split())


type Kind = Literal["numeric", "text", "timestamptz", "boolean"]

_INSTRUMENT = "instrument.ticker instrument.name instrument.isin instrument.currency"
_ORDER_NUMERIC = "id quantity filledQuantity value filledValue limitPrice stopPrice"
_ORDER_TEXT = f"""ticker {_INSTRUMENT} type side status strategy timeInForce currency
initiatedFrom"""
_RESULT = "priceAvgInvestedValue priceAvgResult priceAvgResultCoef priceAvgValue"
_TAXES = "fill.walletImpact.taxes[]"
_SCHEDULES = "workingSchedules[]"
_TIME_EVENTS = "workingSchedules[].timeEvents[]"
_PIE_INSTRUMENTS = "instruments[]"
_PIE_ISSUES = "instruments[].issues[]"
_PIE_SHARES = "settings.instrumentShares{}"

# A map's entry: (the key's field, the value's field).
MAP_ENTRIES: Mapping[str, tuple[str, str]] = {_PIE_SHARES: ("ticker", "share")}


def _endpoint(
    record_type: str,
    table: str,
    required: str,
    *,
    numeric: str = "",
    text: str = "",
    timestamptz: str = "",
    boolean: str = "",
    children: Mapping[str, str] | None = None,
) -> tuple[EndpointSpec, dict[str, Kind]]:
    """An endpoint's spec and each known path's kind; the known paths are those given a kind."""
    declared: dict[Kind, str] = {
        "numeric": numeric,
        "text": text,
        "timestamptz": timestamptz,
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
    "summary": _endpoint(
        "trading212.summary",
        "trading212_accounts",
        "id currency cash.availableToTrade cash.reservedForOrders cash.inPies",
        numeric="""id totalValue cash.availableToTrade cash.reservedForOrders cash.inPies
        investments.currentValue investments.totalCost investments.realizedProfitLoss
        investments.unrealizedProfitLoss""",
        text="currency",
    ),
    "positions": _endpoint(
        "trading212.position",
        "trading212_positions",
        "instrument.ticker instrument.isin instrument.currency quantity",
        numeric="""quantity quantityAvailableForTrading quantityInPies averagePricePaid
        currentPrice walletImpact.totalCost walletImpact.currentValue
        walletImpact.unrealizedProfitLoss walletImpact.fxImpact""",
        text=f"{_INSTRUMENT} walletImpact.currency",
        timestamptz="createdAt",
    ),
    "pending_orders": _endpoint(
        "trading212.pending_order",
        "trading212_pending_orders",
        "id ticker side status",
        numeric=_ORDER_NUMERIC,
        text=_ORDER_TEXT,
        timestamptz="createdAt",
        boolean="extendedHours",
    ),
    "orders": _endpoint(
        "trading212.order",
        "trading212_orders",
        f"""order.id order.side order.status order.instrument.ticker order.instrument.isin
        order.instrument.currency {_under(f"{_TAXES}.", "name quantity currency")}""",
        numeric=f"""{_under("order.", _ORDER_NUMERIC)} fill.id fill.price fill.quantity
        fill.walletImpact.fxRate fill.walletImpact.netValue fill.walletImpact.realisedProfitLoss
        {_TAXES}.quantity""",
        text=f"""{_under("order.", _ORDER_TEXT)} fill.tradingMethod fill.type
        fill.walletImpact.currency {_under(f"{_TAXES}.", "name currency")}""",
        timestamptz=f"order.createdAt fill.filledAt {_TAXES}.chargedAt",
        boolean="order.extendedHours",
        children={_TAXES: "trading212_order_taxes"},
    ),
    "dividends": _endpoint(
        "trading212.dividend",
        "trading212_dividends",
        "reference amount currency paidOn instrument.ticker instrument.isin instrument.currency",
        numeric="amount amountInEuro grossAmountPerShare quantity",
        text=f"reference ticker {_INSTRUMENT} type currency tickerCurrency",
        timestamptz="paidOn",
    ),
    "transactions": _endpoint(
        "trading212.transaction",
        "trading212_transactions",
        "reference type amount currency dateTime",
        numeric="amount",
        text="reference type currency",
        timestamptz="dateTime",
    ),
    "instruments": _endpoint(
        "trading212.instrument",
        "trading212_instruments",
        "ticker type",
        numeric="maxOpenQuantity workingScheduleId",
        text="ticker name shortName isin currencyCode type",
        timestamptz="addedOn",
        boolean="extendedHours",
    ),
    "exchanges": _endpoint(
        "trading212.exchange",
        "trading212_exchanges",
        f"id {_SCHEDULES}.id",
        numeric=f"id {_SCHEDULES}.id",
        text=f"name {_TIME_EVENTS}.type",
        timestamptz=f"{_TIME_EVENTS}.date",
        children={
            _SCHEDULES: "trading212_exchange_schedules",
            _TIME_EVENTS: "trading212_exchange_time_events",
        },
    ),
    "pies": _endpoint(
        "trading212.pie",
        "trading212_pies",
        "id",
        numeric=f"""id cash progress {_under("result.", _RESULT)}
        {_under("dividendDetails.", "gained inCash reinvested")}""",
        text="status",
    ),
    # One pie's detail; it fills the settings of the pies row the list made.
    "pie": _endpoint(
        "trading212.pie_detail",
        "trading212_pies",
        f"settings.id {_PIE_INSTRUMENTS}.ticker {_PIE_SHARES}.ticker {_PIE_SHARES}.share",
        numeric=f"""{_under(f"{_PIE_INSTRUMENTS}.", "ownedQuantity currentShare expectedShare")}
        {_under(f"{_PIE_INSTRUMENTS}.result.", _RESULT)}
        {_under("settings.", "id goal initialInvestment")} {_PIE_SHARES}.share""",
        text=f"""{_PIE_INSTRUMENTS}.ticker {_under(f"{_PIE_ISSUES}.", "name severity")}
        {_under("settings.", "name icon dividendCashAction publicUrl")} {_PIE_SHARES}.ticker""",
        timestamptz=f"{_under('settings.', 'creationDate endDate')}",
        children={
            _PIE_INSTRUMENTS: "trading212_pie_instruments",
            _PIE_ISSUES: "trading212_pie_instrument_issues",
            _PIE_SHARES: "trading212_pie_instrument_shares",
        },
    ),
}

ENDPOINTS: Mapping[str, EndpointSpec] = {name: spec for name, (spec, _) in _DECLARED.items()}
# Endpoint → known path → the type of its column (Decision 3).
KINDS: Mapping[str, Mapping[str, Kind]] = {name: kinds for name, (_, kinds) in _DECLARED.items()}

# Stored typed, but never put in a log, warning or message. Trading 212 returns none.
PERSONAL: frozenset[str] = frozenset()

# Each table's row key, as migration 0030 declares it.
KEYS: Mapping[str, tuple[str, ...]] = {
    "trading212_accounts": ("connection_id", "as_of_date"),
    "trading212_positions": ("connection_id", "as_of_date", "instrument_ticker"),
    "trading212_pending_orders": ("connection_id", "as_of_date", "id"),
    "trading212_orders": ("connection_id", "external_id"),
    "trading212_order_taxes": ("connection_id", "external_id", "item_index"),
    "trading212_dividends": ("connection_id", "reference"),
    "trading212_transactions": ("connection_id", "reference"),
    "trading212_instruments": ("connection_id", "ticker"),
    "trading212_exchanges": ("connection_id", "id"),
    "trading212_exchange_schedules": ("connection_id", "exchange_id", "item_index"),
    "trading212_exchange_time_events": (
        "connection_id",
        "exchange_id",
        "schedule_index",
        "item_index",
    ),
    "trading212_pies": ("connection_id", "id", "as_of_date"),
    "trading212_pie_instruments": ("connection_id", "pie_id", "as_of_date", "item_index"),
    "trading212_pie_instrument_issues": (
        "connection_id",
        "pie_id",
        "as_of_date",
        "instrument_index",
        "item_index",
    ),
    "trading212_pie_instrument_shares": ("connection_id", "pie_id", "as_of_date", "ticker"),
}

# Columns of the tables that are not a field: references, keys and links. A field whose
# column were one of these would overwrite it.
OWN_COLUMNS = frozenset(
    {
        "connection_id",
        "raw_record_id",
        "detail_raw_record_id",
        "account_id",
        "account_kind",
        "as_of_date",
        "transaction_id",
        "snapshot_id",
        "instrument_id",
        "external_id",
        "exchange_id",
        "schedule_index",
        "pie_id",
        "instrument_index",
        "item_index",
    }
)


def split(spec: EndpointSpec, path: str) -> tuple[str, str]:
    """The table a known path's value goes to, and the path below that table's row."""
    owner = max((c for c in spec.children if path.startswith(f"{c}.")), key=len, default=None)
    if owner is None:
        return spec.table, path
    return spec.children[owner], path.removeprefix(f"{owner}.")


def _columns() -> dict[str, dict[str, dict[str, str]]]:
    """Endpoint → table → known path → column. Built through `column_map`, per table across
    every endpoint writing it, so two paths that would share a column fail at import."""
    by_table: dict[str, set[str]] = {}
    for spec in ENDPOINTS.values():
        unknown = spec.required - spec.known
        if unknown:
            raise ValueError(f"{spec.record_type} requires unknown {sorted(unknown)}")
        for path in spec.known:
            table, below = split(spec, path)
            by_table.setdefault(table, set()).add(below)
    table_columns = {table: column_map(sorted(paths)) for table, paths in by_table.items()}
    for table, mapping in table_columns.items():
        if table not in KEYS:
            raise ValueError(f"{table} has no row key")
        if clash := OWN_COLUMNS & set(mapping.values()):
            raise ValueError(f"{table}: a field would be column {', '.join(sorted(clash))}")
    return {name: _endpoint_columns(spec, table_columns) for name, spec in ENDPOINTS.items()}


def _endpoint_columns(
    spec: EndpointSpec, table_columns: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for path in sorted(spec.known):
        table, below = split(spec, path)
        result.setdefault(table, {})[path] = table_columns[table][below]
    return result


# Endpoint → table → known path → column (Decision 3: `column_name` of the path below the row).
COLUMNS: Mapping[str, Mapping[str, Mapping[str, str]]] = _columns()
