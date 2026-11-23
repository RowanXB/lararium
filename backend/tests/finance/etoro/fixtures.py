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
        "counterparty": {"name": "Synthetic Counterparty", "type": "internal_account"},
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
