"""Synthetic eToro API bodies: invented ids, symbols, amounts and dates only.

Builders return JSON-shaped dicts whose numbers are Decimal; `dumps` writes them as the API
would, every number a JSON number exactly as given.
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

GCID = 90000001
REAL_CID = 90000002
CASH_ACCOUNT = "0f9e8d7c-1111-4222-8333-944455556666"
# The 16 read scopes the owner's Read key holds (the probe; scope names, no personal values).
OWNER_READ_SCOPES = [
    f"etoro-public:{name}:read"
    for name in (
        "agent-portfolio club crypto feed market-data money.balance money.cash-transactions"
        " notifications pi-data pnl-alerts sso-applications sso-scopes trade.demo trade.real"
        " user-info watchlist"
    ).split()
]
READ_SCOPES = [
    "etoro-public:trade.real:read",
    "etoro-public:money.balance:read",
    "etoro-public:money.cash-transactions:read",
    "etoro-public:market-data:read",
]


def dumps(value: Any) -> str:
    """JSON text, Decimals as exact numbers."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Decimal | int):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(dumps(v) for v in value) + "]"
    raise TypeError(type(value))


def me_json(scopes: list[str] | None = None) -> dict[str, Any]:
    return {
        "gcid": GCID,
        "realCid": REAL_CID,
        "demoCid": 90000003,
        "username": "synthetic-user",
        "firstName": "Testa",
        "middleName": None,
        "lastName": "Example",
        "playerLevel": 1,
        "gender": 0,
        "language": 1,
        "dateOfBirth": "1990-01-01T00:00:00",
        "avatarUrl": None,
        "scopes": READ_SCOPES if scopes is None else scopes,
    }


def trade_row(position_id: int, close: date = date(2026, 6, 1)) -> dict[str, Any]:
    return {
        "netProfit": Decimal("1.25"),
        "closeRate": Decimal("101.5"),
        "closeTimestamp": f"{close.isoformat()}T14:30:00.123Z",
        "positionId": position_id,
        "instrumentId": 9001,
        "isBuy": True,
        "leverage": 1,
        "openRate": Decimal("100.0"),
        "openTimestamp": "2026-05-01T09:00:00.000Z",
        "stopLossRate": Decimal("0.0001"),
        "takeProfitRate": Decimal("0"),
        "trailingStopLoss": False,
        "orderId": position_id + 500,
        "socialTradeId": 0,
        "parentPositionId": 0,
        "investment": Decimal("100.0"),
        "initialInvestment": Decimal("100.0"),
        "fees": Decimal("0.0"),
        "units": Decimal("1.0"),
    }


def cash_item(item_id: str, amount: str = "10.00") -> dict[str, Any]:
    return {
        "id": item_id,
        "accountId": CASH_ACCOUNT,
        "transactionType": "internalTransfer",
        "transactionSubtype": "transferReceived",
        "direction": "credit",
        "status": "settled",
        "amount": amount,
        "currency": "GBP",
        "originalAmount": None,
        "originalCurrency": None,
        "conversionRate": None,
        "postedAt": "2026-06-01T10:16:12.1234567Z",
        "counterparty": {"name": "Synthetic Trading Account", "type": "internal_account"},
        "cardTransactionDetails": None,
        "bankTransferTransactionDetails": None,
        "internalTransferTransactionDetails": {"transferId": "70000001"},
    }


def cash_page(items: list[dict[str, Any]], token: str | None = None) -> dict[str, Any]:
    return {
        "results": items,
        "pagination": {"pageSize": 500, "nextPageToken": token, "hasNext": token is not None},
    }


def data_page(items: list[dict[str, Any]], page: int, has_next: bool) -> dict[str, Any]:
    return {"results": items, "pagination": {"page": page, "pageSize": 500, "hasNext": has_next}}


def candle(day: date) -> dict[str, Any]:
    return {
        "time": f"{day.isoformat()}T00:00:00Z",
        "open": "100.10",
        "high": "101.20",
        "low": "99.30",
        "close": "100.40",
        "volume": "12345",
    }


def candles_page(items: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {
        "instrumentId": 9001,
        "symbol": "ZZQA",
        "interval": "1d",
        "side": "bid",
        "window": {"from": "2026-06-01T00:00:00Z", "to": "2026-06-05T00:00:00Z"},
        "pagination": {"limit": 2000, "hasNext": cursor is not None, "nextCursor": cursor},
        "results": items,
    }


# The normalizer's inputs (test_normalize_api): one Trading, one eToro Money and one MoneyFarm
# account, and a stock listed in USD.
TRADING_ACCOUNT = "12345678"
MONEYFARM_ACCOUNT = "23456789"
INSTRUMENT_ID = 9001
SYMBOL = "ZZQA"


def balance_entry(
    account_id: str, kind: str, currency: str, balance: str, sub: str | None = None
) -> dict[str, Any]:
    return {
        "accountId": account_id,
        "accountType": kind,
        "subType": sub,
        "balance": Decimal(balance),
        "currency": currency,
        "displayBalance": Decimal(balance),
        "displayCurrency": "USD",
        "exchangeRate": Decimal("1"),
        "equityDetails": {"available": Decimal(balance)},
    }


def balances_body(
    *, money: str = "0", moneyfarm: str = "0", extra: tuple[dict[str, Any], ...] = ()
) -> dict[str, Any]:
    return {
        "gcid": GCID,
        "displayCurrency": "USD",
        "totalBalance": Decimal("0"),
        "balances": [
            balance_entry(TRADING_ACCOUNT, "Trading", "USD", "0"),
            balance_entry(CASH_ACCOUNT, "Cash", "GBP", money),
            balance_entry(MONEYFARM_ACCOUNT, "MoneyFarm", "GBP", moneyfarm, "ISA"),
            *extra,
        ],
    }


def pnl_position(
    position_id: int,
    *,
    units: str = "4",
    amount: str = "500.00",
    instrument: int = INSTRUMENT_ID,
    opened: str = "2026-05-01T09:00:00.000Z",
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "positionID": position_id,
        "CID": REAL_CID,
        "instrumentID": instrument,
        "orderID": position_id + 500,
        "openDateTime": opened,
        "isBuy": True,
        "units": Decimal(units),
        "initialUnits": Decimal(units),
        "amount": Decimal(amount),
        "initialAmountInDollars": Decimal(amount),
        "openRate": Decimal("125.0"),
        "openConversionRate": Decimal("1"),
        "leverage": 1,
        "mirrorID": 0,
        "parentPositionID": 0,
        "settlementTypeID": 1,
        "totalFees": Decimal("0"),
        **overrides,
    }


def pnl_body(positions: list[dict[str, Any]], credit: str = "0") -> dict[str, Any]:
    return {
        "clientPortfolio": {
            "accountCurrencyId": 1,
            "credit": Decimal(credit),
            "bonusCredit": Decimal("0"),
            "unrealizedPnL": Decimal("0"),
            "positions": positions,
        }
    }


def aggregate_body(instruments: list[tuple[int, str, str]]) -> dict[str, Any]:
    """(instrument id, asset currency, net units) per instrument."""
    return {
        "accountCurrency": "USD",
        "cid": REAL_CID,
        "accountTotals": {"accountTotalValue": Decimal("0"), "accountAvailableCash": Decimal("0")},
        "instrumentAggregates": [
            {"instrumentId": i, "assetCurrency": currency, "netUnits": Decimal(units)}
            for i, currency, units in instruments
        ],
    }


def breakdown_position(
    position_id: int,
    *,
    units: str = "4",
    instrument: int = INSTRUMENT_ID,
    currency: str = "USD",
    rate: str = "126.5",
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "positionId": position_id,
        "instrumentId": instrument,
        "symbol": SYMBOL,
        "units": Decimal(units),
        "assetCurrency": currency,
        "settlementType": "real",
        "leverage": 1,
        "direction": "long",
        "currentRate": Decimal(rate),
        "openTime": "2026-05-01T09:00:00.000",
        **overrides,
    }


def breakdown_body(positions: list[dict[str, Any]]) -> dict[str, Any]:
    by_instrument: dict[Any, list[dict[str, Any]]] = {}
    for position in positions:
        by_instrument.setdefault(position["instrumentId"], []).append(position)
    return {
        "accountCurrency": "USD",
        "timestamp": "2026-06-02T12:00:00.0000000Z",
        "instruments": [
            {"instrumentId": i, "symbol": SYMBOL, "positions": rows}
            for i, rows in by_instrument.items()
        ],
    }


def lookup_body(
    order_id: int,
    position_id: int,
    *,
    instrument: int = INSTRUMENT_ID,
    currency: str = "USD",
    fees: str = "0",
    taxes: str = "0",
    settlement: str = "REAL",
) -> dict[str, Any]:
    return {
        "orderId": order_id,
        "action": "open",
        "transaction": "buy",
        "asset": {
            "instrumentId": instrument,
            "symbol": SYMBOL,
            "currency": currency,
            "settlementType": settlement,
            "leverage": 1,
            "side": "long",
        },
        "positionExecutions": [
            {
                "positionId": position_id,
                "state": "open",
                "remainingUnits": Decimal("4"),
                "openingData": {
                    "orderId": order_id,
                    "units": Decimal("4"),
                    "avgPrice": Decimal("125.0"),
                    "avgConversionRate": Decimal("1"),
                    "marketSpread": Decimal("0.05"),
                    "markup": Decimal("0.02"),
                    "fees": Decimal(fees),
                    "taxes": Decimal(taxes),
                    "openTime": "2026-05-01T09:00:00.0Z",
                },
            }
        ],
        "positionsToClose": [],
    }


def instrument_meta(
    instrument: int = INSTRUMENT_ID,
    type_id: int = 5,
    symbol: str = SYMBOL,
    name: str = "Synthetic Quality A",
) -> dict[str, Any]:
    return {
        "instrumentID": instrument,
        "instrumentDisplayName": name,
        "instrumentTypeID": type_id,
        "exchangeID": 4,
        "symbolFull": symbol,
        "priceSource": "Synthetic",
        "hasExpirationDate": False,
        "isInternalInstrument": False,
        "images": [{"instrumentID": instrument, "width": Decimal("50"), "uri": "https://x.test/i"}],
    }


def bank_item(
    item_id: str,
    amount: str,
    direction: str = "credit",
    identifier: tuple[str, str] = ("iban", "GB33ZZZZ04000412345678"),
    status: str = "settled",
) -> dict[str, Any]:
    name, value = identifier
    return {
        **cash_item(item_id, amount),
        "transactionType": "bankTransfer",
        "transactionSubtype": "paymentReceived" if direction == "credit" else "payment",
        "direction": direction,
        "status": status,
        "counterparty": {"name": "Synthetic Payer Name", "type": "bank_account"},
        "internalTransferTransactionDetails": None,
        "bankTransferTransactionDetails": {
            "bankIdentifier": [{"name": name, "value": value}],
            "description": None,
            "paymentReference": "SYNTHREF01",
        },
    }


def history_body(snapshots: list[tuple[date, list[tuple[str, str, str, str]]]]) -> dict[str, Any]:
    """Each day's (account id, type, currency, cash)."""
    days = [d for d, _ in snapshots]
    return {
        "gcid": GCID,
        "displayCurrency": "USD",
        "fromDate": min(days).isoformat(),
        "toDate": max(days).isoformat(),
        "snapshots": [
            {
                "date": day.isoformat(),
                "totalCurrencyIso": "USD",
                "totalCash": Decimal("0"),
                "accountSnapshots": [
                    {
                        "accountId": account_id,
                        "accountType": kind,
                        "currency": currency,
                        "cash": Decimal(cash),
                        "total": Decimal(cash),
                    }
                    for account_id, kind, currency, cash in accounts
                ],
            }
            for day, accounts in snapshots
        ],
    }


def candle_item(day: date, close: str, instrument: int = INSTRUMENT_ID) -> dict[str, Any]:
    return {**candle(day), "close": close, "instrumentId": instrument}
