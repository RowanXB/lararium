"""Synthetic Trading 212 API bodies: invented account ids, tickers, ISINs and amounts only.

Builders return JSON-shaped dicts whose numbers are Decimal; `dumps` writes them as the API
would, every number a JSON number exactly as given.
"""

import json
from decimal import Decimal
from typing import Any

ACCOUNT_ID = 10000001
TICKER = "ZZQA_US_EQ"


def isin(body: str) -> str:
    """`body` (11 characters) with its ISIN check digit."""
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for position, char in enumerate(reversed(digits)):
        n = int(char) * (2 if position % 2 == 0 else 1)
        total += n // 10 + n % 10
    return f"{body}{(10 - total % 10) % 10}"


ISIN = isin("US00ZZQA000")


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


def page(items: list[dict[str, Any]], next_page_path: str | None = None) -> dict[str, Any]:
    return {"items": items, "nextPagePath": next_page_path}


def instrument(ticker: str = TICKER, isin_code: str = ISIN, currency: str = "USD") -> dict:
    return {"ticker": ticker, "name": "Zzqa Example Corp", "isin": isin_code, "currency": currency}


def summary_json(account_id: int = ACCOUNT_ID, currency: str = "GBP", **overrides: Any) -> dict:
    return {
        "id": account_id,
        "currency": currency,
        "totalValue": Decimal("1250.75"),
        "cash": {
            "availableToTrade": Decimal("200.50"),
            "reservedForOrders": Decimal("0"),
            "inPies": Decimal("50.25"),
        },
        "investments": {
            "currentValue": Decimal("1000.00"),
            "totalCost": Decimal("900.00"),
            "realizedProfitLoss": Decimal("12.34"),
            "unrealizedProfitLoss": Decimal("100.00"),
        },
        **overrides,
    }


def position_json(ticker: str = TICKER, quantity: str = "3", **overrides: Any) -> dict:
    return {
        "instrument": instrument(ticker),
        "quantity": Decimal(quantity),
        "quantityAvailableForTrading": Decimal(quantity),
        "quantityInPies": Decimal("0"),
        "averagePricePaid": Decimal("100.00"),
        "currentPrice": Decimal("110.00"),
        "createdAt": "2026-03-02T14:30:00Z",
        "walletImpact": {
            "currency": "GBP",
            "totalCost": Decimal("240.00"),
            "currentValue": Decimal("264.00"),
            "unrealizedProfitLoss": Decimal("24.00"),
            "fxImpact": None,
        },
        **overrides,
    }


def order_item(
    order_id: int = 500001,
    fill_id: int | None = 700001,
    side: str = "BUY",
    quantity: str = "3",
    price: str = "100.00",
    net_value: str = "241.20",
    taxes: list[dict] | None = None,
) -> dict:
    order = {
        "id": order_id,
        "createdAt": "2026-03-02T14:29:58Z",
        "ticker": TICKER,
        "instrument": instrument(),
        "type": "MARKET",
        "side": side,
        "status": "FILLED" if fill_id is not None else "CANCELLED",
        "strategy": "QUANTITY",
        "quantity": Decimal(quantity),
        "filledQuantity": Decimal(quantity) if fill_id is not None else Decimal("0"),
        "currency": "GBP",
        "extendedHours": False,
        "initiatedFrom": "WEB",
    }
    if fill_id is None:
        return {"order": order, "fill": None}
    fill = {
        "id": fill_id,
        "filledAt": "2026-03-02T14:30:00Z",
        "price": Decimal(price),
        "quantity": Decimal(quantity),
        "tradingMethod": "TOTV",
        "type": "TRADE",
        "walletImpact": {
            "currency": "GBP",
            "fxRate": Decimal("0.8"),
            "netValue": Decimal(net_value),
            "realisedProfitLoss": Decimal("0"),
            "taxes": taxes
            if taxes is not None
            else [
                {
                    "name": "CURRENCY_CONVERSION_FEE",
                    "quantity": Decimal("1.20"),
                    "currency": "GBP",
                    "chargedAt": "2026-03-02T14:30:00Z",
                }
            ],
        },
    }
    return {"order": order, "fill": fill}


def dividend_item(reference: str = "div-0001", amount: str = "1.23", **overrides: Any) -> dict:
    return {
        "ticker": TICKER,
        "instrument": instrument(),
        "type": "ORDINARY",
        "amount": Decimal(amount),
        "currency": "GBP",
        "amountInEuro": Decimal("1.41"),
        "grossAmountPerShare": Decimal("0.51"),
        "tickerCurrency": "USD",
        "quantity": Decimal("3"),
        "paidOn": "2026-04-01T00:00:00Z",
        "reference": reference,
        **overrides,
    }


def transaction_item(
    reference: str = "00000000-0000-4000-8000-000000000001",
    kind: str = "DEPOSIT",
    amount: str = "250.00",
    **overrides: Any,
) -> dict:
    return {
        "type": kind,
        "amount": Decimal(amount),
        "currency": "GBP",
        "dateTime": "2026-03-01T09:15:00Z",
        "reference": reference,
        **overrides,
    }


def instrument_json(ticker: str = TICKER, **overrides: Any) -> dict:
    return {
        "ticker": ticker,
        "name": "Zzqa Example Corp",
        "shortName": "ZZQA",
        "isin": ISIN,
        "currencyCode": "USD",
        "type": "STOCK",
        "maxOpenQuantity": Decimal("1000"),
        "extendedHours": True,
        "workingScheduleId": 71,
        "addedOn": "2020-01-15T07:00:00Z",
        **overrides,
    }


def pie_json(pie_id: int = 9001, **overrides: Any) -> dict:
    """A pie of the pie list (`GET /equity/pies`)."""
    result = {
        "priceAvgInvestedValue": Decimal("50.00"),
        "priceAvgResult": Decimal("2.50"),
        "priceAvgResultCoef": Decimal("0.05"),
        "priceAvgValue": Decimal("52.50"),
    }
    return {
        "id": pie_id,
        "cash": Decimal("1.25"),
        "progress": Decimal("0.5"),
        "status": "ON_TRACK",
        "result": result,
        "dividendDetails": {
            "gained": Decimal("0.40"),
            "inCash": Decimal("0.10"),
            "reinvested": Decimal("0.30"),
        },
        **overrides,
    }
