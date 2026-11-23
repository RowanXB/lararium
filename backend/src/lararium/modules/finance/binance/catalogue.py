"""The Binance read-only catalogue: every request M7 may send, one `Endpoint` per label.

The client's allowlist is computed from `ENDPOINTS`, so an endpoint that is not listed here
can never be sent. `FIELDS` gives each label's response fields; a test holds both to the
catalogue document (`docs/superpowers/specs/2026-11-22-binance-endpoint-fields.md`), so
neither drifts.

A field's path is dotted and starts at the response body: `[]` marks an array level (a
leading `[]` means the body itself is an array), `<name>` stands for any key of a map, and
`[0]` for a position in a bar. Where the records are is `Endpoint.rows`: `""` when the body
is one record, `"."` when it is an array of records, otherwise the path of the array.
"""

import re
from dataclasses import dataclass
from typing import Literal

type FieldType = Literal[
    "decimal", "integer", "id", "boolean", "time_ms", "time_text", "text", "object", "array"
]


@dataclass(frozen=True)
class Field:
    path: str
    type: FieldType
    required: bool = False
    enum: frozenset[str] | None = None
    # The element's contents vary (Binance's `filters`): its keys beyond those listed are
    # not reported as unknown.
    opaque: bool = False


@dataclass(frozen=True)
class Endpoint:
    label: str
    method: Literal["GET", "POST"]
    host: str
    path: str
    weight: int
    limit: Literal["api", "fapi", "dapi", "eapi", "papi", "ip", "uid"]
    signed: bool
    # Where the records are, and which of their fields name one and date it (relative to a row).
    rows: str
    id_fields: tuple[str, ...]
    time_field: str | None
    # The longest span of one request, in days, and how far back Binance answers; `None` when
    # the documents give none.
    window_days: int | None
    lookback_days: int | None
    time_params: tuple[str, str]
    paging: Literal["none", "offset", "page", "from_id"]
    # The `limit` or page size to ask for; 0 when the endpoint takes none.
    page_size: int
    # A parameter and the values to ask for, one request each.
    variants: tuple[tuple[str, tuple[str, ...]], ...]
    # public: reference data; account: totals and flags; balance and position: what the
    # account holds now; history: records in time.
    kind: Literal["public", "account", "balance", "position", "history"]
    phase: Literal["A", "B"]


_HOSTS = {
    "fapi": "fapi.binance.com",
    "dapi": "dapi.binance.com",
    "eapi": "eapi.binance.com",
    "papi": "papi.binance.com",
}
_TIME_PARAMS = ("startTime", "endTime")


def _ep(
    label: str,
    method: Literal["GET", "POST"],
    path: str,
    weight: int,
    limit: Literal["api", "fapi", "dapi", "eapi", "papi", "ip", "uid"],
    *,
    signed: bool = True,
    rows: str = "",
    ids: tuple[str, ...] = (),
    time: str | None = None,
    window: int | None = None,
    lookback: int | None = None,
    params: tuple[str, str] = _TIME_PARAMS,
    paging: Literal["none", "offset", "page", "from_id"] = "none",
    page: int = 0,
    variants: tuple[tuple[str, tuple[str, ...]], ...] = (),
    kind: Literal["public", "account", "balance", "position", "history"],
    phase: Literal["A", "B"] = "B",
) -> Endpoint:
    return Endpoint(
        label=label,
        method=method,
        host=_HOSTS.get(limit, "api.binance.com"),
        path=path,
        weight=weight,
        limit=limit,
        signed=signed,
        rows=rows,
        id_fields=ids,
        time_field=time,
        window_days=window,
        lookback_days=lookback,
        time_params=params,
        paging=paging,
        page_size=page,
        variants=variants,
        kind=kind,
        phase=phase,
    )


ENDPOINTS: dict[str, Endpoint] = {
    "account-info": _ep(
        "account-info", "GET", "/sapi/v1/account/info", 1, "ip", kind="account", phase="A"
    ),
    "account-status": _ep(
        "account-status", "GET", "/sapi/v1/account/status", 1, "ip", kind="account", phase="A"
    ),
    "api-restrictions": _ep(
        "api-restrictions",
        "GET",
        "/sapi/v1/account/apiRestrictions",
        1,
        "ip",
        time="createTime",
        kind="account",
        phase="A",
    ),
    "api-trading-status": _ep(
        "api-trading-status",
        "GET",
        "/sapi/v1/account/apiTradingStatus",
        1,
        "ip",
        time="data.updateTime",
        kind="account",
        phase="A",
    ),
    "asset-detail": _ep(
        "asset-detail", "GET", "/sapi/v1/asset/assetDetail", 1, "ip", kind="public", phase="A"
    ),
    "bfusd-account": _ep(
        "bfusd-account", "GET", "/sapi/v1/bfusd/account", 150, "ip", kind="balance"
    ),
    "bfusd-redemptions": _ep(
        "bfusd-redemptions",
        "GET",
        "/sapi/v1/bfusd/history/redemptionHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "bfusd-rewards": _ep(
        "bfusd-rewards",
        "GET",
        "/sapi/v1/bfusd/history/rewardsHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "bfusd-subscriptions": _ep(
        "bfusd-subscriptions",
        "GET",
        "/sapi/v1/bfusd/history/subscriptionHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "bnsol-rewards": _ep(
        "bnsol-rewards",
        "GET",
        "/sapi/v1/sol-staking/sol/history/bnsolRewardsHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "c2c": _ep(
        "c2c",
        "GET",
        "/sapi/v1/c2c/orderMatch/listUserOrderHistory",
        1,
        "ip",
        rows="data",
        ids=("orderNumber",),
        time="createTime",
        window=29,
        lookback=180,
        params=("startTimestamp", "endTimestamp"),
        paging="page",
        page=100,
        variants=(("tradeType", ("BUY", "SELL")),),
        kind="history",
    ),
    "cloud-mining": _ep(
        "cloud-mining",
        "GET",
        "/sapi/v1/asset/ledger-transfer/cloud-mining/queryByPage",
        600,
        "uid",
        rows="rows",
        ids=("tranId",),
        time="createTime",
        paging="page",
        page=100,
        kind="history",
    ),
    "cm-account": _ep(
        "cm-account",
        "GET",
        "/dapi/v1/account",
        5,
        "dapi",
        rows="assets",
        ids=("asset",),
        time="updateTime",
        kind="balance",
    ),
    "cm-balance": _ep(
        "cm-balance",
        "GET",
        "/dapi/v1/balance",
        1,
        "dapi",
        rows=".",
        ids=("asset",),
        time="updateTime",
        kind="balance",
    ),
    "cm-income": _ep(
        "cm-income",
        "GET",
        "/dapi/v1/income",
        20,
        "dapi",
        rows=".",
        ids=("incomeType", "tranId"),
        time="time",
        window=364,
        paging="none",
        page=1000,
        kind="history",
    ),
    "cm-positions": _ep(
        "cm-positions",
        "GET",
        "/dapi/v1/positionRisk",
        1,
        "dapi",
        rows=".",
        ids=("symbol", "positionSide"),
        time="updateTime",
        kind="position",
    ),
    "cm-trades": _ep(
        "cm-trades",
        "GET",
        "/dapi/v1/userTrades",
        20,
        "dapi",
        rows=".",
        ids=("symbol", "id"),
        time="time",
        window=6,
        lookback=90,
        paging="none",
        page=1000,
        kind="history",
    ),
    "coin-config": _ep(
        "coin-config",
        "GET",
        "/sapi/v1/capital/config/getall",
        10,
        "ip",
        rows=".",
        ids=("coin",),
        kind="public",
        phase="A",
    ),
    "convert": _ep(
        "convert",
        "GET",
        "/sapi/v1/convert/tradeFlow",
        3000,
        "uid",
        rows="list",
        ids=("orderId",),
        time="createTime",
        window=29,
        paging="none",
        page=1000,
        kind="history",
        phase="A",
    ),
    "dci-accounts": _ep(
        "dci-accounts", "GET", "/sapi/v1/dci/product/accounts", 1, "ip", kind="account"
    ),
    "dci-positions": _ep(
        "dci-positions",
        "GET",
        "/sapi/v1/dci/product/positions",
        1,
        "ip",
        rows="list",
        ids=("id",),
        time="subscriptionTime",
        paging="page",
        page=100,
        kind="position",
    ),
    "deposits": _ep(
        "deposits",
        "GET",
        "/sapi/v1/capital/deposit/hisrec",
        1,
        "ip",
        rows=".",
        ids=("id",),
        time="insertTime",
        window=89,
        paging="offset",
        page=1000,
        kind="history",
        phase="A",
    ),
    "discount-accounts": _ep(
        "discount-accounts",
        "GET",
        "/sapi/v1/accumulator/product/sum-holding",
        1,
        "ip",
        kind="account",
    ),
    "discount-positions": _ep(
        "discount-positions",
        "GET",
        "/sapi/v1/accumulator/product/position/list",
        1,
        "ip",
        rows="list",
        ids=("id",),
        paging="page",
        page=100,
        kind="position",
    ),
    "dividends": _ep(
        "dividends",
        "GET",
        "/sapi/v1/asset/assetDividend",
        10,
        "ip",
        rows="rows",
        ids=("id",),
        time="divTime",
        window=179,
        paging="none",
        page=500,
        kind="history",
        phase="A",
    ),
    "dust": _ep(
        "dust",
        "GET",
        "/sapi/v1/asset/dribblet",
        1,
        "ip",
        rows="userAssetDribblets",
        ids=("transId",),
        time="operateTime",
        variants=(("accountType", ("SPOT", "MARGIN")),),
        kind="history",
    ),
    "earn-account": _ep(
        "earn-account", "GET", "/sapi/v1/simple-earn/account", 150, "ip", kind="account", phase="A"
    ),
    "earn-flexible-collateral": _ep(
        "earn-flexible-collateral",
        "GET",
        "/sapi/v1/simple-earn/flexible/history/collateralRecord",
        1,
        "ip",
        rows="rows",
        ids=("orderId",),
        time="createTime",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "earn-flexible-position": _ep(
        "earn-flexible-position",
        "GET",
        "/sapi/v1/simple-earn/flexible/position",
        150,
        "ip",
        rows="rows",
        ids=("productId",),
        paging="page",
        page=100,
        kind="position",
        phase="A",
    ),
    "earn-flexible-redemptions": _ep(
        "earn-flexible-redemptions",
        "GET",
        "/sapi/v1/simple-earn/flexible/history/redemptionRecord",
        150,
        "ip",
        rows="rows",
        ids=("redeemId",),
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
        phase="A",
    ),
    "earn-flexible-rewards": _ep(
        "earn-flexible-rewards",
        "GET",
        "/sapi/v1/simple-earn/flexible/history/rewardsRecord",
        150,
        "ip",
        rows="rows",
        ids=(),
        time="time",
        window=29,
        paging="page",
        page=100,
        # Binance refuses the request without `type` (-1102), whatever its documentation's
        # "Default: ALL" says.
        variants=(("type", ("ALL",)),),
        kind="history",
        phase="A",
    ),
    "earn-flexible-subscriptions": _ep(
        "earn-flexible-subscriptions",
        "GET",
        "/sapi/v1/simple-earn/flexible/history/subscriptionRecord",
        150,
        "ip",
        rows="rows",
        ids=("purchaseId",),
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
        phase="A",
    ),
    "earn-locked-position": _ep(
        "earn-locked-position",
        "GET",
        "/sapi/v1/simple-earn/locked/position",
        150,
        "ip",
        rows="rows",
        ids=("positionId",),
        time="purchaseTime",
        paging="page",
        page=100,
        kind="position",
    ),
    "earn-locked-redemptions": _ep(
        "earn-locked-redemptions",
        "GET",
        "/sapi/v1/simple-earn/locked/history/redemptionRecord",
        150,
        "ip",
        rows="rows",
        ids=("redeemId",),
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
        phase="B",
    ),
    "earn-locked-rewards": _ep(
        "earn-locked-rewards",
        "GET",
        "/sapi/v1/simple-earn/locked/history/rewardsRecord",
        150,
        "ip",
        rows="rows",
        ids=(),
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
        phase="B",
    ),
    "earn-locked-subscriptions": _ep(
        "earn-locked-subscriptions",
        "GET",
        "/sapi/v1/simple-earn/locked/history/subscriptionRecord",
        150,
        "ip",
        rows="rows",
        ids=("purchaseId",),
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
        phase="B",
    ),
    "eth-redemptions": _ep(
        "eth-redemptions",
        "GET",
        "/sapi/v1/eth-staking/eth/history/redemptionHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "eth-stakes": _ep(
        "eth-stakes",
        "GET",
        "/sapi/v1/eth-staking/eth/history/stakingHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "eth-staking-account": _ep(
        "eth-staking-account", "GET", "/sapi/v2/eth-staking/account", 150, "ip", kind="balance"
    ),
    "exchange-info": _ep(
        "exchange-info",
        "GET",
        "/api/v3/exchangeInfo",
        20,
        "api",
        signed=False,
        rows="symbols",
        ids=("symbol",),
        kind="public",
        phase="A",
    ),
    "fiat-orders": _ep(
        "fiat-orders",
        "GET",
        "/sapi/v1/fiat/orders",
        45000,
        "uid",
        rows="data",
        ids=("orderNo",),
        time="createTime",
        params=("beginTime", "endTime"),
        paging="page",
        page=500,
        variants=(("transactionType", ("0", "1")),),
        kind="history",
    ),
    "fiat-payments": _ep(
        "fiat-payments",
        "GET",
        "/sapi/v1/fiat/payments",
        1,
        "ip",
        rows="data",
        ids=("orderNo",),
        time="createTime",
        params=("beginTime", "endTime"),
        paging="page",
        page=500,
        variants=(("transactionType", ("0", "1")),),
        kind="history",
    ),
    "funding-asset": _ep(
        "funding-asset",
        "POST",
        "/sapi/v1/asset/get-funding-asset",
        1,
        "ip",
        rows=".",
        ids=("asset",),
        kind="balance",
        phase="A",
    ),
    "klines": _ep(
        "klines",
        "GET",
        "/api/v3/klines",
        2,
        "api",
        signed=False,
        rows=".",
        ids=("0",),
        time="0",
        window=999,
        paging="none",
        page=1000,
        variants=(("interval", ("1d",)),),
        kind="public",
        phase="A",
    ),
    "loan-flexible-borrows": _ep(
        "loan-flexible-borrows",
        "GET",
        "/sapi/v2/loan/flexible/borrow/history",
        400,
        "ip",
        rows="rows",
        time="borrowTime",
        window=179,
        paging="page",
        page=100,
        kind="history",
    ),
    "loan-flexible-liquidations": _ep(
        "loan-flexible-liquidations",
        "GET",
        "/sapi/v2/loan/flexible/liquidation/history",
        400,
        "ip",
        rows="rows",
        time="liquidationStartingTime",
        paging="page",
        page=100,
        kind="history",
    ),
    "loan-flexible-ltv-adjustments": _ep(
        "loan-flexible-ltv-adjustments",
        "GET",
        "/sapi/v2/loan/flexible/ltv/adjustment/history",
        400,
        "uid",
        rows="rows",
        time="adjustTime",
        window=179,
        paging="page",
        page=100,
        kind="history",
    ),
    "loan-flexible-ongoing": _ep(
        "loan-flexible-ongoing",
        "GET",
        "/sapi/v2/loan/flexible/ongoing/orders",
        300,
        "ip",
        rows="rows",
        paging="page",
        page=100,
        kind="position",
    ),
    "loan-flexible-repays": _ep(
        "loan-flexible-repays",
        "GET",
        "/sapi/v2/loan/flexible/repay/history",
        400,
        "ip",
        rows="rows",
        time="repayTime",
        window=179,
        paging="page",
        page=100,
        kind="history",
    ),
    "margin-account": _ep(
        "margin-account",
        "GET",
        "/sapi/v1/margin/account",
        10,
        "ip",
        rows="userAssets",
        ids=("asset",),
        kind="balance",
    ),
    "margin-borrow-repay": _ep(
        "margin-borrow-repay",
        "GET",
        "/sapi/v1/margin/borrow-repay",
        10,
        "ip",
        rows="rows",
        ids=("txId",),
        time="timestamp",
        window=6,
        paging="page",
        page=100,
        variants=(("type", ("BORROW", "REPAY")),),
        kind="history",
    ),
    "margin-capital-flow": _ep(
        "margin-capital-flow",
        "GET",
        "/sapi/v1/margin/capital-flow",
        100,
        "ip",
        rows=".",
        ids=("id",),
        time="timestamp",
        window=6,
        lookback=90,
        paging="from_id",
        page=1000,
        kind="history",
    ),
    "margin-interest": _ep(
        "margin-interest",
        "GET",
        "/sapi/v1/margin/interestHistory",
        1,
        "ip",
        rows="rows",
        ids=("txId", "interestAccuredTime", "asset"),
        time="interestAccuredTime",
        window=29,
        lookback=90,
        paging="page",
        page=100,
        kind="history",
    ),
    "margin-isolated-account": _ep(
        "margin-isolated-account",
        "GET",
        "/sapi/v1/margin/isolated/account",
        10,
        "ip",
        rows="assets",
        ids=("symbol",),
        kind="balance",
    ),
    "margin-liquidation-loan": _ep(
        "margin-liquidation-loan",
        "GET",
        "/sapi/v1/margin/liquidation-loan",
        100,
        "uid",
        ids=("asset",),
        kind="position",
    ),
    "margin-liquidation-loan-repays": _ep(
        "margin-liquidation-loan-repays",
        "GET",
        "/sapi/v1/margin/liquidation-loan/repay-history",
        100,
        "uid",
        rows="rows",
        ids=("repayId",),
        time="createTime",
        window=89,
        lookback=90,
        paging="page",
        page=50,
        kind="history",
    ),
    "margin-liquidations": _ep(
        "margin-liquidations",
        "GET",
        "/sapi/v1/margin/forceLiquidationRec",
        1,
        "ip",
        rows="rows",
        ids=("orderId",),
        time="updatedTime",
        paging="page",
        page=100,
        kind="history",
    ),
    "margin-small-liability": _ep(
        "margin-small-liability",
        "GET",
        "/sapi/v1/margin/exchange-small-liability-history",
        100,
        "uid",
        rows="rows",
        time="timestamp",
        paging="page",
        page=100,
        kind="history",
    ),
    "margin-trades": _ep(
        "margin-trades",
        "GET",
        "/sapi/v1/margin/myTrades",
        10,
        "ip",
        rows=".",
        ids=("symbol", "id"),
        time="time",
        paging="from_id",
        page=1000,
        kind="history",
    ),
    "margin-transfers": _ep(
        "margin-transfers",
        "GET",
        "/sapi/v1/margin/transfer",
        1,
        "ip",
        rows="rows",
        ids=("txId",),
        time="timestamp",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "my-trades": _ep(
        "my-trades",
        "GET",
        "/api/v3/myTrades",
        20,
        "api",
        rows=".",
        ids=("symbol", "id"),
        time="time",
        paging="from_id",
        page=1000,
        kind="history",
        phase="A",
    ),
    "onchain-account": _ep(
        "onchain-account", "GET", "/sapi/v1/onchain-yields/account", 50, "ip", kind="account"
    ),
    "onchain-locked-position": _ep(
        "onchain-locked-position",
        "GET",
        "/sapi/v1/onchain-yields/locked/position",
        50,
        "ip",
        rows="rows",
        ids=("positionId",),
        time="purchaseTime",
        paging="page",
        page=100,
        kind="position",
    ),
    "onchain-redemptions": _ep(
        "onchain-redemptions",
        "GET",
        "/sapi/v1/onchain-yields/locked/history/redemptionRecord",
        50,
        "ip",
        rows="rows",
        ids=("redeemId",),
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "onchain-rewards": _ep(
        "onchain-rewards",
        "GET",
        "/sapi/v1/onchain-yields/locked/history/rewardsRecord",
        50,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "onchain-subscriptions": _ep(
        "onchain-subscriptions",
        "GET",
        "/sapi/v1/onchain-yields/locked/history/subscriptionRecord",
        50,
        "ip",
        rows="rows",
        ids=("purchaseId",),
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "opt-account": _ep(
        "opt-account",
        "GET",
        "/eapi/v1/marginAccount",
        3,
        "eapi",
        rows="asset",
        ids=("asset",),
        kind="balance",
    ),
    "opt-bill": _ep(
        "opt-bill",
        "GET",
        "/eapi/v1/bill",
        1,
        "eapi",
        rows=".",
        ids=("id",),
        time="createDate",
        lookback=90,
        paging="from_id",
        page=1000,
        variants=(("currency", ("USDT",)),),
        kind="history",
    ),
    "opt-exercises": _ep(
        "opt-exercises",
        "GET",
        "/eapi/v1/exerciseRecord",
        5,
        "eapi",
        rows=".",
        ids=("id",),
        time="createDate",
        paging="none",
        page=1000,
        kind="history",
    ),
    "opt-positions": _ep(
        "opt-positions",
        "GET",
        "/eapi/v1/position",
        5,
        "eapi",
        rows=".",
        ids=("symbol", "side"),
        time="time",
        kind="position",
    ),
    "opt-trades": _ep(
        "opt-trades",
        "GET",
        "/eapi/v1/userTrades",
        5,
        "eapi",
        rows=".",
        ids=("id",),
        time="time",
        lookback=90,
        paging="from_id",
        page=1000,
        kind="history",
    ),
    "pay": _ep(
        "pay",
        "GET",
        "/sapi/v1/pay/transactions",
        3000,
        "uid",
        rows="data",
        ids=("transactionId",),
        time="transactionTime",
        window=89,
        lookback=540,
        paging="none",
        page=100,
        kind="history",
    ),
    "pm-account": _ep("pm-account", "GET", "/papi/v1/account", 20, "papi", kind="account"),
    "pm-balance": _ep(
        "pm-balance",
        "GET",
        "/papi/v1/balance",
        20,
        "papi",
        rows=".",
        ids=("asset",),
        time="updateTime",
        kind="balance",
    ),
    "rebate": _ep(
        "rebate",
        "GET",
        "/sapi/v1/rebate/taxQuery",
        12000,
        "uid",
        rows="data.data",
        time="updateTime",
        window=29,
        paging="page",
        page=200,
        kind="history",
    ),
    "rwusd-account": _ep(
        "rwusd-account", "GET", "/sapi/v1/rwusd/account", 150, "ip", kind="balance"
    ),
    "rwusd-redemptions": _ep(
        "rwusd-redemptions",
        "GET",
        "/sapi/v1/rwusd/history/redemptionHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "rwusd-rewards": _ep(
        "rwusd-rewards",
        "GET",
        "/sapi/v1/rwusd/history/rewardsHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "rwusd-subscriptions": _ep(
        "rwusd-subscriptions",
        "GET",
        "/sapi/v1/rwusd/history/subscriptionHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=29,
        paging="page",
        page=100,
        kind="history",
    ),
    "snapshot": _ep(
        "snapshot",
        "GET",
        "/sapi/v1/accountSnapshot",
        2400,
        "ip",
        rows="snapshotVos",
        ids=("type", "updateTime"),
        time="updateTime",
        window=29,
        lookback=30,
        paging="none",
        page=30,
        variants=(("type", ("SPOT", "MARGIN", "FUTURES")),),
        kind="history",
        phase="A",
    ),
    "soft-staking-list": _ep(
        "soft-staking-list",
        "GET",
        "/sapi/v1/soft-staking/list",
        50,
        "ip",
        rows="rows",
        ids=("asset",),
        paging="page",
        page=100,
        kind="position",
    ),
    "soft-staking-rewards": _ep(
        "soft-staking-rewards",
        "GET",
        "/sapi/v1/soft-staking/history/rewardsRecord",
        50,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "sol-boost-rewards": _ep(
        "sol-boost-rewards",
        "GET",
        "/sapi/v1/sol-staking/sol/history/boostRewardsHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        variants=(("type", ("CLAIM", "DISTRIBUTE")),),
        kind="history",
    ),
    "sol-redemptions": _ep(
        "sol-redemptions",
        "GET",
        "/sapi/v1/sol-staking/sol/history/redemptionHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "sol-stakes": _ep(
        "sol-stakes",
        "GET",
        "/sapi/v1/sol-staking/sol/history/stakingHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "sol-staking-account": _ep(
        "sol-staking-account", "GET", "/sapi/v1/sol-staking/account", 150, "ip", kind="balance"
    ),
    "sol-unclaimed-rewards": _ep(
        "sol-unclaimed-rewards",
        "GET",
        "/sapi/v1/sol-staking/sol/history/unclaimedRewards",
        150,
        "ip",
        rows=".",
        ids=("rewardsAsset",),
        kind="position",
    ),
    "spot-account": _ep(
        "spot-account",
        "GET",
        "/api/v3/account",
        20,
        "api",
        rows="balances",
        ids=("asset",),
        kind="balance",
        phase="A",
    ),
    "time": _ep(
        "time",
        "GET",
        "/api/v3/time",
        1,
        "api",
        signed=False,
        time="serverTime",
        kind="public",
        phase="A",
    ),
    "travel-deposits": _ep(
        "travel-deposits",
        "GET",
        "/sapi/v1/localentity/deposit/history",
        1,
        "ip",
        rows=".",
        ids=("trId",),
        time="insertTime",
        window=89,
        paging="offset",
        page=1000,
        kind="history",
    ),
    "travel-withdrawals": _ep(
        "travel-withdrawals",
        "GET",
        "/sapi/v1/localentity/withdraw/history",
        1,
        "ip",
        rows=".",
        ids=("id",),
        time="applyTime",
        window=89,
        paging="offset",
        page=1000,
        kind="history",
    ),
    "um-account": _ep(
        "um-account",
        "GET",
        "/fapi/v3/account",
        5,
        "fapi",
        rows="assets",
        ids=("asset",),
        time="updateTime",
        kind="balance",
    ),
    "um-balance": _ep(
        "um-balance",
        "GET",
        "/fapi/v3/balance",
        5,
        "fapi",
        rows=".",
        ids=("asset",),
        time="updateTime",
        kind="balance",
    ),
    "um-income": _ep(
        "um-income",
        "GET",
        "/fapi/v1/income",
        30,
        "fapi",
        rows=".",
        ids=("incomeType", "tranId", "asset", "symbol"),
        time="time",
        lookback=90,
        paging="none",
        page=1000,
        kind="history",
    ),
    "um-positions": _ep(
        "um-positions",
        "GET",
        "/fapi/v3/positionRisk",
        5,
        "fapi",
        rows=".",
        ids=("symbol", "positionSide"),
        time="updateTime",
        kind="position",
    ),
    "um-trades": _ep(
        "um-trades",
        "GET",
        "/fapi/v1/userTrades",
        5,
        "fapi",
        rows=".",
        ids=("symbol", "id"),
        time="time",
        window=6,
        lookback=90,
        paging="none",
        page=1000,
        kind="history",
    ),
    "universal-transfer": _ep(
        "universal-transfer",
        "GET",
        "/sapi/v1/asset/transfer",
        1,
        "ip",
        rows="rows",
        ids=("tranId",),
        time="timestamp",
        lookback=180,
        paging="page",
        page=100,
        variants=(
            (
                "type",
                (
                    "MAIN_UMFUTURE",
                    "MAIN_CMFUTURE",
                    "MAIN_MARGIN",
                    "UMFUTURE_MAIN",
                    "UMFUTURE_MARGIN",
                    "CMFUTURE_MAIN",
                    "CMFUTURE_MARGIN",
                    "MARGIN_MAIN",
                    "MARGIN_UMFUTURE",
                    "MARGIN_CMFUTURE",
                    "MAIN_FUNDING",
                    "FUNDING_MAIN",
                    "FUNDING_UMFUTURE",
                    "UMFUTURE_FUNDING",
                    "MARGIN_FUNDING",
                    "FUNDING_MARGIN",
                    "FUNDING_CMFUTURE",
                    "CMFUTURE_FUNDING",
                    "MAIN_OPTION",
                    "OPTION_MAIN",
                    "UMFUTURE_OPTION",
                    "OPTION_UMFUTURE",
                    "MARGIN_OPTION",
                    "OPTION_MARGIN",
                    "FUNDING_OPTION",
                    "OPTION_FUNDING",
                    "MAIN_PORTFOLIO_MARGIN",
                    "PORTFOLIO_MARGIN_MAIN",
                ),
            ),
        ),
        kind="history",
    ),
    "user-asset": _ep(
        "user-asset",
        "POST",
        "/sapi/v3/asset/getUserAsset",
        5,
        "ip",
        rows=".",
        ids=("asset",),
        kind="balance",
        phase="A",
    ),
    "vip-loan-collateral-account": _ep(
        "vip-loan-collateral-account",
        "GET",
        "/sapi/v1/loan/vip/collateral/account",
        6000,
        "ip",
        rows="rows",
        ids=("collateralAccountId",),
        paging="page",
        page=100,
        kind="position",
    ),
    "vip-loan-interest": _ep(
        "vip-loan-interest",
        "GET",
        "/sapi/v1/loan/vip/accruedInterest",
        400,
        "ip",
        rows="rows",
        time="accrualTime",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "vip-loan-ongoing": _ep(
        "vip-loan-ongoing",
        "GET",
        "/sapi/v1/loan/vip/ongoing/orders",
        400,
        "ip",
        rows="rows",
        ids=("orderId",),
        time="loanDate",
        paging="page",
        page=100,
        kind="position",
    ),
    "vip-loan-repays": _ep(
        "vip-loan-repays",
        "GET",
        "/sapi/v1/loan/vip/repay/history",
        400,
        "ip",
        rows="rows",
        time="repayTime",
        window=179,
        paging="page",
        page=100,
        kind="history",
    ),
    "vip-loan-requests": _ep(
        "vip-loan-requests",
        "GET",
        "/sapi/v1/loan/vip/request/data",
        400,
        "uid",
        rows="rows",
        ids=("requestId",),
        time="loanDate",
        paging="page",
        page=100,
        kind="history",
    ),
    "wallet-balance": _ep(
        "wallet-balance",
        "GET",
        "/sapi/v1/asset/wallet/balance",
        60,
        "ip",
        rows=".",
        ids=("walletName",),
        kind="balance",
        phase="A",
    ),
    "wbeth-rewards": _ep(
        "wbeth-rewards",
        "GET",
        "/sapi/v1/eth-staking/eth/history/wbethRewardsHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "wbeth-unwraps": _ep(
        "wbeth-unwraps",
        "GET",
        "/sapi/v1/eth-staking/wbeth/history/unwrapHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "wbeth-wraps": _ep(
        "wbeth-wraps",
        "GET",
        "/sapi/v1/eth-staking/wbeth/history/wrapHistory",
        150,
        "ip",
        rows="rows",
        time="time",
        window=89,
        paging="page",
        page=100,
        kind="history",
    ),
    "withdrawals": _ep(
        "withdrawals",
        "GET",
        "/sapi/v1/capital/withdraw/history",
        18000,
        "uid",
        rows=".",
        ids=("id",),
        time="applyTime",
        window=89,
        paging="offset",
        page=1000,
        kind="history",
        phase="A",
    ),
}


def _field_type(name: str) -> FieldType:
    match name:
        case (
            "decimal"
            | "integer"
            | "id"
            | "boolean"
            | "time_ms"
            | "time_text"
            | "text"
            | "object"
            | "array"
        ):
            return name
        case _:
            raise ValueError(f"unknown field type {name!r}")


def _fields(spec: str) -> tuple[Field, ...]:
    """One field per line: `<path> <type>[!][~] [= A|B|C]`.

    `!` marks a field every record has, `~` an opaque element, and `= A|B|C` the values the
    documents list for it.
    """
    fields: list[Field] = []
    # A long list of values continues on the next line after a trailing `|`.
    for line in re.sub(r"\|\n\s*", "|", spec.strip()).splitlines():
        head, _, values = line.partition(" = ")
        path, token = head.split(" ")
        fields.append(
            Field(
                path=path,
                type=_field_type(token.rstrip("!~")),
                required="!" in token,
                enum=frozenset(values.split("|")) if values else None,
                opaque="~" in token,
            )
        )
    return tuple(fields)


_FIELD_SPECS = {
    "account-info": """
vipLevel integer!
isMarginEnabled boolean!
isFutureEnabled boolean!
isOptionsEnabled boolean!
isPortfolioMarginRetailEnabled boolean!
""",
    "account-status": """
data text!
""",
    "api-restrictions": """
ipRestrict boolean
createTime time_ms
enableReading boolean
enableWithdrawals boolean
enableInternalTransfer boolean
enableMargin boolean
enableFutures boolean
permitsUniversalTransfer boolean
enableVanillaOptions boolean
enableFixApiTrade boolean
enableFixReadOnly boolean
enableSpotAndMarginTrading boolean
enablePortfolioMarginTrading boolean
""",
    "api-trading-status": """
data object!
data.isLocked boolean
data.plannedRecoverTime time_ms
data.triggerCondition object
data.triggerCondition.GCR integer
data.triggerCondition.IFER integer
data.triggerCondition.UFR integer
data.updateTime time_ms
""",
    "asset-detail": """
<ASSET>.minWithdrawAmount decimal
<ASSET>.depositStatus boolean
<ASSET>.withdrawFee decimal
<ASSET>.withdrawStatus boolean
<ASSET>.depositTip text
""",
    "bfusd-account": """
bfusdAmount decimal!
usdtProfit decimal!
bfusdProfit decimal!
""",
    "bfusd-redemptions": """
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].receiveAsset text!
rows[].receiveAmount decimal!
rows[].fee decimal
rows[].arrivalTime time_ms
rows[].status text = PENDING|SUCCESS
total integer
""",
    "bfusd-rewards": """
rows[].time time_ms!
rows[].rewardAsset text
rows[].rewardsAmount decimal!
rows[].BFUSDPosition decimal
rows[].annualPercentageRate decimal
total integer
""",
    "bfusd-subscriptions": """
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].receiveAsset text!
rows[].receiveAmount decimal!
rows[].status text
total integer
""",
    "bnsol-rewards": """
estRewardsInSOL decimal
rows[].time time_ms!
rows[].amountInSOL decimal
rows[].holding decimal
rows[].holdingInSOL decimal
rows[].annualPercentageRate decimal
total integer
""",
    "c2c": """
code text
message text
data[].orderNumber text!
data[].advNo text
data[].tradeType text = BUY|SELL
data[].asset text!
data[].fiat text
data[].fiatSymbol text
data[].amount decimal!
data[].totalPrice decimal
data[].unitPrice decimal
data[].orderStatus text = PENDING|TRADING|BUYER_PAYED|DISTRIBUTING|COMPLETED|IN_APPEAL|
    CANCELLED|CANCELLED_BY_SYSTEM
data[].createTime time_ms!
data[].commission decimal
data[].counterPartNickName text
data[].payMethodName text
data[].additionalKycVerify integer
data[].takerCommissionRate decimal
data[].takerCommission decimal
data[].takerAmount decimal
data[].advertisementRole text = MAKER|TAKER
total integer
success boolean
""",
    "cloud-mining": """
total integer
rows[].createTime time_ms!
rows[].tranId id!
rows[].type integer = 248|249
rows[].asset text!
rows[].amount decimal!
rows[].status text = S
""",
    "cm-account": """
assets[].asset text!
assets[].walletBalance decimal!
assets[].unrealizedProfit decimal
assets[].marginBalance decimal
assets[].maintMargin decimal
assets[].initialMargin decimal
assets[].positionInitialMargin decimal
assets[].openOrderInitialMargin decimal
assets[].maxWithdrawAmount decimal
assets[].crossWalletBalance decimal
assets[].crossUnPnl decimal
assets[].availableBalance decimal
assets[].updateTime time_ms!
positions[].symbol text
positions[].positionAmt decimal
positions[].initialMargin decimal
positions[].maintMargin decimal
positions[].unrealizedProfit decimal
positions[].positionInitialMargin decimal
positions[].openOrderInitialMargin decimal
positions[].leverage text
positions[].isolated boolean
positions[].positionSide text = BOTH|LONG|SHORT
positions[].entryPrice decimal
positions[].breakEvenPrice decimal
positions[].maxQty decimal
positions[].updateTime time_ms
positions[].notionalValue decimal
positions[].isolatedWallet decimal
canDeposit boolean
canTrade boolean
canWithdraw boolean
feeTier integer
updateTime time_ms
""",
    "cm-balance": """
[].accountAlias text
[].asset text!
[].balance decimal!
[].withdrawAvailable decimal
[].crossWalletBalance decimal
[].crossUnPnl decimal
[].availableBalance decimal
[].updateTime time_ms!
""",
    "cm-income": """
[].symbol text
[].incomeType text! = TRANSFER|WELCOME_BONUS|FUNDING_FEE|REALIZED_PNL|COMMISSION|
    INSURANCE_CLEAR|DELIVERED_SETTELMENT
[].income decimal!
[].asset text!
[].info text
[].time time_ms!
[].tranId text!
[].tradeId text
""",
    "cm-positions": """
[].symbol text!
[].positionAmt decimal!
[].entryPrice decimal
[].breakEvenPrice decimal
[].markPrice decimal
[].unRealizedProfit decimal
[].liquidationPrice decimal
[].leverage text
[].maxQty decimal
[].marginType text
[].isolatedMargin decimal
[].isAutoAddMargin text
[].positionSide text! = BOTH|LONG|SHORT
[].updateTime time_ms!
[].isolatedWallet decimal
[].notionalValue decimal
""",
    "cm-trades": """
[].symbol text!
[].id id!
[].orderId id
[].pair text
[].side text
[].price decimal!
[].qty decimal!
[].realizedPnl decimal
[].marginAsset text
[].baseQty decimal
[].quoteQty decimal!
[].commission decimal!
[].commissionAsset text!
[].time time_ms!
[].positionSide text
[].buyer boolean
[].maker boolean
""",
    "coin-config": """
[].coin text!
[].depositAllEnable boolean
[].withdrawAllEnable boolean
[].name text
[].free decimal
[].locked decimal
[].freeze decimal
[].withdrawing decimal
[].ipoing decimal
[].ipoable decimal
[].storage decimal
[].isLegalMoney boolean
[].trading boolean
[].networkList[].network text
[].networkList[].coin text
[].networkList[].withdrawIntegerMultiple decimal
[].networkList[].isDefault boolean
[].networkList[].depositEnable boolean
[].networkList[].withdrawEnable boolean
[].networkList[].depositDesc text
[].networkList[].withdrawDesc text
[].networkList[].specialTips text
[].networkList[].specialWithdrawTips text
[].networkList[].name text
[].networkList[].resetAddressStatus boolean
[].networkList[].addressRegex text
[].networkList[].memoRegex text
[].networkList[].withdrawFee decimal
[].networkList[].withdrawMin decimal
[].networkList[].withdrawMax decimal
[].networkList[].withdrawInternalMin decimal
[].networkList[].depositDust decimal
[].networkList[].minConfirm integer
[].networkList[].unLockConfirm integer
[].networkList[].sameAddress boolean
[].networkList[].withdrawTag boolean
[].networkList[].estimatedArrivalTime integer
[].networkList[].busy boolean
[].networkList[].contractAddressUrl text
[].networkList[].contractAddress text
[].networkList[].denomination integer
""",
    "convert": """
list[].quoteId text
list[].orderId id!
list[].orderStatus text
list[].fromAsset text!
list[].fromAmount decimal!
list[].toAsset text!
list[].toAmount decimal!
list[].ratio decimal
list[].inverseRatio decimal
list[].createTime time_ms!
list[].orderType text
list[].side text
list[].walletType text
startTime time_ms
endTime time_ms
limit integer
moreData boolean
""",
    "dci-accounts": """
totalAmountInBTC decimal!
totalAmountInUSDT decimal!
""",
    "dci-positions": """
total integer
list[].id text!
list[].investCoin text
list[].exercisedCoin text
list[].subscriptionAmount decimal
list[].strikePrice decimal
list[].duration integer
list[].settleDate time_ms
list[].purchaseStatus text = PENDING|PURCHASE_SUCCESS|SETTLED|PURCHASE_FAIL|REFUNDING|
    REFUND_SUCCESS|SETTLING
list[].apr decimal
list[].orderId id
list[].purchaseEndTime time_ms
list[].optionType text
list[].autoCompoundPlan text = NULL|STANDARD|ADVANCED
list[].subscriptionTime time_ms!
""",
    "deposits": """
[].id text!
[].amount decimal!
[].coin text!
[].network text
[].status integer = 0|1|2|6|7|8
[].address text
[].addressTag text
[].txId text
[].insertTime time_ms!
[].completeTime time_ms
[].transferType integer
[].confirmTimes text
[].unlockConfirm integer
[].walletType integer
[].travelRuleStatus integer = 0|1
[].sourceAddress text
""",
    "discount-accounts": """
totalAmountInBTC decimal!
totalAmountInUSDT decimal!
""",
    "discount-positions": """
total integer
list[].id text!
list[].investAsset text
list[].targetAsset text
list[].depositAmount decimal
list[].strikePrice decimal
list[].knockoutPrice decimal
list[].duration integer
list[].settleDate time_ms
list[].purchaseStatus text = PENDING|PURCHASE_SUCCESS|SETTLED|PURCHASE_FAIL|REFUNDING|
    REFUND_SUCCESS|SETTLING
list[].knockoutApr decimal
list[].orderId id
""",
    "dividends": """
rows[].id id!
rows[].amount decimal!
rows[].asset text!
rows[].divTime time_ms!
rows[].enInfo text
rows[].tranId id
rows[].direction integer = 1|-1
total integer
""",
    "dust": """
total integer
userAssetDribblets[].operateTime time_ms!
userAssetDribblets[].totalTransferedAmount decimal
userAssetDribblets[].totalServiceChargeAmount decimal
userAssetDribblets[].transId id!
userAssetDribblets[].userAssetDribbletDetails[].transId id
userAssetDribblets[].userAssetDribbletDetails[].serviceChargeAmount decimal
userAssetDribblets[].userAssetDribbletDetails[].amount decimal
userAssetDribblets[].userAssetDribbletDetails[].operateTime time_ms
userAssetDribblets[].userAssetDribbletDetails[].transferedAmount decimal
userAssetDribblets[].userAssetDribbletDetails[].fromAsset text
userAssetDribblets[].userAssetDribbletDetails[].targetAsset text
""",
    "earn-account": """
totalAmountInBTC decimal!
totalAmountInUSDT decimal!
totalFlexibleAmountInBTC decimal!
totalFlexibleAmountInUSDT decimal!
totalLockedInBTC decimal!
totalLockedInUSDT decimal!
""",
    "earn-flexible-collateral": """
rows[].amount decimal!
rows[].productId text
rows[].asset text!
rows[].createTime time_ms!
rows[].type text
rows[].productName text
rows[].orderId id!
total text
""",
    "earn-flexible-position": """
rows[].totalAmount decimal
rows[].tierAnnualPercentageRate object
rows[].tierAnnualPercentageRate.<tier> decimal
rows[].latestAnnualPercentageRate decimal
rows[].yesterdayAirdropPercentageRate decimal
rows[].asset text!
rows[].airDropAsset text
rows[].canRedeem boolean
rows[].collateralAmount decimal
rows[].productId text!
rows[].yesterdayRealTimeRewards decimal
rows[].cumulativeBonusRewards decimal
rows[].cumulativeRealTimeRewards decimal
rows[].cumulativeTotalRewards decimal
rows[].autoSubscribe boolean
total integer
""",
    "earn-flexible-redemptions": """
rows[].amount decimal!
rows[].asset text!
rows[].time time_ms!
rows[].productId text
rows[].redeemId id!
rows[].destAccount text = SPOT|FUNDING
rows[].status text = CREATED|PAID|FAILED
total integer
""",
    "earn-flexible-rewards": """
rows[].asset text!
rows[].rewards decimal!
rows[].productId text
rows[].type text = BONUS|REALTIME|REWARDS
rows[].time time_ms!
total integer
""",
    "earn-flexible-subscriptions": """
rows[].amount decimal!
rows[].asset text!
rows[].time time_ms!
rows[].purchaseId id!
rows[].productId text
rows[].type text = AUTO|NORMAL|CONVERT|LOAN|AI|TRANSFER
rows[].sourceAccount text = SPOT|FUNDING|SPOTANDFUNDING
rows[].amtFromSpot decimal
rows[].amtFromFunding decimal
rows[].status text = PURCHASING|SUCCESS|FAILED
total integer
""",
    "earn-locked-position": """
rows[].positionId id!
rows[].parentPositionId id
rows[].projectId text
rows[].asset text!
rows[].amount decimal!
rows[].purchaseTime time_ms!
rows[].duration text
rows[].accrualDays text
rows[].rewardAsset text
rows[].APY decimal
rows[].rewardAmt decimal
rows[].extraRewardAsset text
rows[].extraRewardAPR decimal
rows[].estExtraRewardAmt decimal
rows[].boostRewardAsset text
rows[].boostApr decimal
rows[].totalBoostRewardAmt decimal
rows[].nextPay decimal
rows[].nextPayDate time_ms
rows[].payPeriod text
rows[].redeemAmountEarly decimal
rows[].rewardsEndDate time_ms
rows[].deliverDate time_ms
rows[].redeemPeriod text
rows[].redeemingAmt decimal
rows[].redeemTo text
rows[].partialAmtDeliverDate time_ms
rows[].canRedeemEarly boolean
rows[].canFastRedemption boolean
rows[].autoSubscribe boolean
rows[].type text
rows[].status text
rows[].canReStake boolean
total integer
""",
    "earn-locked-redemptions": """
rows[].positionId id
rows[].redeemId id!
rows[].time time_ms!
rows[].asset text!
rows[].lockPeriod text
rows[].amount decimal!
rows[].originalAmount decimal
rows[].type text = MATURE|NEW_TRANSFERRED|AHEAD
rows[].deliverDate time_ms
rows[].lossAmount decimal
rows[].isComplete boolean
rows[].rewardAsset text
rows[].rewardAmt decimal
rows[].extraRewardAsset text
rows[].estExtraRewardAmt decimal
rows[].status text
total integer
""",
    "earn-locked-rewards": """
rows[].positionId id
rows[].time time_ms!
rows[].asset text!
rows[].lockPeriod text
rows[].amount decimal!
rows[].type text
total integer
""",
    "earn-locked-subscriptions": """
rows[].positionId id
rows[].purchaseId text!
rows[].projectId text
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].lockPeriod text
rows[].type text = NORMAL|AUTO|ACTIVITY|TRIAL|RESTAKE
rows[].sourceAccount text = SPOT|FUNDING|SPOTANDFUNDING
rows[].amtFromSpot decimal
rows[].amtFromFunding decimal
rows[].status text = PURCHASING|SUCCESS|FAILED
total integer
""",
    "eth-redemptions": """
rows[].time time_ms!
rows[].arrivalTime time_ms
rows[].asset text!
rows[].amount decimal!
rows[].distributeAsset text!
rows[].distributeAmount decimal!
rows[].conversionRatio decimal
rows[].status text = PENDING|SUCCESS|FAILED
total integer
""",
    "eth-stakes": """
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].distributeAsset text!
rows[].distributeAmount decimal!
rows[].conversionRatio decimal
rows[].status text = PENDING|SUCCESS|FAILED
total integer
""",
    "eth-staking-account": """
holdingInETH decimal!
holdings.wbethAmount decimal
holdings.bethAmount decimal
thirtyDaysProfitInETH decimal!
profit.amountFromWBETH decimal
profit.amountFromBETH decimal
""",
    "exchange-info": """
timezone text
serverTime time_ms
rateLimits[] array
rateLimits[].rateLimitType text
rateLimits[].interval text
rateLimits[].intervalNum integer
rateLimits[].limit integer
rateLimits[].count integer
exchangeFilters[] array~
exchangeFilters[].filterType text
exchangeFilters[].maxNumOrders integer
exchangeFilters[].maxNumAlgoOrders integer
exchangeFilters[].maxNumIcebergOrders integer
exchangeFilters[].maxNumOrderLists integer
symbols[].symbol text!
symbols[].status text = TRADING|HALT|BREAK|CANCEL_ONLY
symbols[].baseAsset text!
symbols[].baseAssetPrecision integer
symbols[].quoteAsset text!
symbols[].quotePrecision integer
symbols[].quoteAssetPrecision integer
symbols[].baseCommissionPrecision integer
symbols[].quoteCommissionPrecision integer
symbols[].orderTypes[] text
symbols[].icebergAllowed boolean
symbols[].ocoAllowed boolean
symbols[].otoAllowed boolean
symbols[].opoAllowed boolean
symbols[].quoteOrderQtyMarketAllowed boolean
symbols[].allowTrailingStop boolean
symbols[].cancelReplaceAllowed boolean
symbols[].amendAllowed boolean
symbols[].pegInstructionsAllowed boolean
symbols[].marketQuoteNotionalLimitAllowed boolean
symbols[].limitQuoteNotionalLimitAllowed boolean
symbols[].isSpotTradingAllowed boolean
symbols[].isMarginTradingAllowed boolean
symbols[].filters[] array~
symbols[].filters[].filterType text
symbols[].permissions[] text
symbols[].permissionSets[][] text
symbols[].defaultSelfTradePreventionMode text
symbols[].allowedSelfTradePreventionModes[] text
sors[] array
sors[].baseAsset text
sors[].symbols[] text
""",
    "fiat-orders": """
code text
message text
data[].orderNo text!
data[].fiatCurrency text
data[].indicatedAmount decimal
data[].amount decimal!
data[].totalFee decimal
data[].method text
data[].status text = Processing|Failed|Successful|Finished|Refunding|Refunded|
    Refund Failed|Order Partial Credit Stopped
data[].createTime time_ms!
data[].updateTime time_ms
total integer
success boolean
""",
    "fiat-payments": """
code text
message text
data[].orderNo text!
data[].sourceAmount decimal
data[].fiatCurrency text
data[].obtainAmount decimal
data[].cryptoCurrency text
data[].totalFee decimal
data[].price decimal
data[].status text = Processing|Completed|Failed|Refunded
data[].paymentMethod text = Cash Balance|Credit Card|Online Banking|Bank Transfer
data[].createTime time_ms!
data[].updateTime time_ms
total integer
success boolean
""",
    "funding-asset": """
[].asset text!
[].free decimal!
[].locked decimal!
[].freeze decimal
[].withdrawing decimal
[].btcValuation decimal
""",
    "klines": """
[][0] time_ms!
[][1] decimal!
[][2] decimal!
[][3] decimal!
[][4] decimal!
[][5] decimal!
[][6] time_ms!
[][7] decimal!
[][8] integer!
[][9] decimal!
[][10] decimal!
[][11] text
""",
    "loan-flexible-borrows": """
rows[].loanCoin text!
rows[].initialLoanAmount decimal!
rows[].collateralCoin text!
rows[].initialCollateralAmount decimal
rows[].borrowTime time_ms!
rows[].status text = SUCCESS|FAILED|PENDING
total integer
""",
    "loan-flexible-liquidations": """
rows[].loanCoin text!
rows[].liquidationDebt decimal
rows[].collateralCoin text!
rows[].liquidationCollateralAmount decimal
rows[].returnCollateralAmount decimal
rows[].liquidationFee decimal
rows[].liquidationStartingPrice decimal
rows[].liquidationStartingTime time_ms!
rows[].status text
total integer
""",
    "loan-flexible-ltv-adjustments": """
rows[].loanCoin text!
rows[].collateralCoin text!
rows[].direction text
rows[].collateralAmount decimal
rows[].preLTV decimal
rows[].afterLTV decimal
rows[].adjustTime time_ms!
total integer
""",
    "loan-flexible-ongoing": """
rows[].loanCoin text!
rows[].totalDebt decimal!
rows[].collateralCoin text!
rows[].collateralAmount decimal
rows[].currentLTV decimal
total integer
""",
    "loan-flexible-repays": """
rows[].loanCoin text!
rows[].repayAmount decimal!
rows[].collateralCoin text!
rows[].collateralReturn decimal
rows[].repayStatus text = REPAID|REPAYING|FAILED
rows[].repayTime time_ms!
total integer
""",
    "margin-account": """
created boolean
borrowEnabled boolean
marginLevel decimal
collateralMarginLevel decimal
totalAssetOfBtc decimal
totalLiabilityOfBtc decimal
totalNetAssetOfBtc decimal
TotalCollateralValueInUSDT decimal
tradeEnabled boolean
transferInEnabled boolean
transferOutEnabled boolean
transferEnabled boolean
accountType text = MARGIN_1|MARGIN_2
userAssets[].asset text!
userAssets[].borrowed decimal
userAssets[].free decimal!
userAssets[].interest decimal
userAssets[].locked decimal!
userAssets[].netAsset decimal
""",
    "margin-borrow-repay": """
rows[].type text
rows[].isolatedSymbol text
rows[].amount decimal!
rows[].asset text!
rows[].interest decimal
rows[].principal decimal
rows[].status text = PENDING|CONFIRMED|FAILED
rows[].timestamp time_ms!
rows[].txId id!
total integer
""",
    "margin-capital-flow": """
[].id id!
[].tranId id
[].timestamp time_ms!
[].asset text!
[].symbol text
[].type text = TRANSFER|BORROW|REPAY|BUY_INCOME|BUY_EXPENSE|SELL_INCOME|SELL_EXPENSE|
    TRADING_COMMISSION|BUY_LIQUIDATION|SELL_LIQUIDATION|REPAY_LIQUIDATION|OTHER_LIQUIDATION|LIQUIDATION_FEE|SMALL_BALANCE_CONVERT|COMMISSION_RETURN|SMALL_CONVERT
[].amount decimal!
[].note text
""",
    "margin-interest": """
rows[].txId id!
rows[].interestAccuredTime time_ms!
rows[].asset text!
rows[].rawAsset text
rows[].principal decimal
rows[].interest decimal
rows[].interestRate decimal
rows[].type text = PERIODIC|ON_BORROW|PERIODIC_CONVERTED|ON_BORROW_CONVERTED|PORTFOLIO
rows[].isolatedSymbol text
total integer
""",
    "margin-isolated-account": """
assets[].baseAsset object
assets[].baseAsset.asset text
assets[].baseAsset.borrowEnabled boolean
assets[].baseAsset.borrowed decimal
assets[].baseAsset.free decimal
assets[].baseAsset.interest decimal
assets[].baseAsset.locked decimal
assets[].baseAsset.netAsset decimal
assets[].baseAsset.netAssetOfBtc decimal
assets[].baseAsset.repayEnabled boolean
assets[].baseAsset.totalAsset decimal
assets[].quoteAsset object
assets[].quoteAsset.asset text
assets[].quoteAsset.borrowEnabled boolean
assets[].quoteAsset.borrowed decimal
assets[].quoteAsset.free decimal
assets[].quoteAsset.interest decimal
assets[].quoteAsset.locked decimal
assets[].quoteAsset.netAsset decimal
assets[].quoteAsset.netAssetOfBtc decimal
assets[].quoteAsset.repayEnabled boolean
assets[].quoteAsset.totalAsset decimal
assets[].symbol text!
assets[].isolatedCreated boolean
assets[].enabled boolean
assets[].marginLevel decimal
assets[].marginLevelStatus text = EXCESSIVE|NORMAL|MARGIN_CALL|PRE_LIQUIDATION|FORCE_LIQUIDATION
assets[].marginRatio decimal
assets[].indexPrice decimal
assets[].liquidatePrice decimal
assets[].liquidateRate decimal
assets[].tradeEnabled boolean
totalAssetOfBtc decimal
totalLiabilityOfBtc decimal
totalNetAssetOfBtc decimal
""",
    "margin-liquidation-loan": """
asset text
amount decimal
repaidAmount decimal
remainingAmount decimal
""",
    "margin-liquidation-loan-repays": """
total integer
rows[].repayId id!
rows[].asset text!
rows[].amount decimal!
rows[].status text = SUCCESS|PENDING
rows[].createTime time_ms!
""",
    "margin-liquidations": """
rows[].avgPrice decimal
rows[].executedQty decimal
rows[].orderId id!
rows[].price decimal
rows[].qty decimal
rows[].side text
rows[].symbol text
rows[].timeInForce text
rows[].isIsolated boolean
rows[].updatedTime time_ms!
total integer
""",
    "margin-small-liability": """
total integer
rows[].asset text!
rows[].amount decimal!
rows[].targetAsset text
rows[].targetAmount decimal
rows[].bizType text
rows[].timestamp time_ms!
""",
    "margin-trades": """
[].commission decimal!
[].commissionAsset text!
[].id id!
[].isBestMatch boolean
[].isBuyer boolean
[].isMaker boolean
[].orderId id
[].price decimal!
[].qty decimal!
[].symbol text!
[].isIsolated boolean
[].time time_ms!
""",
    "margin-transfers": """
rows[].amount decimal!
rows[].asset text!
rows[].status text
rows[].timestamp time_ms!
rows[].txId id!
rows[].type text = ROLL_IN|ROLL_OUT
rows[].transFrom text = SPOT|FUTURES|FIAT|DELIVERY|MINING|ISOLATED_MARGIN|FUNDING|
    MOTHER_SPOT|OPTION|SUB_SPOT|SUB_MARGIN|CROSS_MARGIN
rows[].transTo text = SPOT|FUTURES|FIAT|DELIVERY|MINING|ISOLATED_MARGIN|FUNDING|
    MOTHER_SPOT|OPTION|SUB_SPOT|SUB_MARGIN|CROSS_MARGIN
rows[].fromSymbol text
rows[].toSymbol text
total integer
""",
    "my-trades": """
[].symbol text!
[].id id!
[].orderId id
[].orderListId id
[].price decimal!
[].qty decimal!
[].quoteQty decimal!
[].commission decimal!
[].commissionAsset text!
[].time time_ms!
[].isBuyer boolean
[].isMaker boolean
[].isBestMatch boolean
""",
    "onchain-account": """
totalAmountInBTC decimal!
totalAmountInUSDT decimal!
totalFlexibleAmountInBTC decimal!
totalFlexibleAmountInUSDT decimal!
totalLockedInBTC decimal!
totalLockedInUSDT decimal!
""",
    "onchain-locked-position": """
rows[].positionId text!
rows[].projectId text
rows[].asset text!
rows[].amount decimal!
rows[].purchaseTime time_text!
rows[].duration text
rows[].accrualDays text
rows[].rewardAsset text
rows[].APY decimal
rows[].rewardAmt decimal
rows[].nextPay decimal
rows[].nextPayDate time_text
rows[].payPeriod text
rows[].rewardsPayDate time_text
rows[].rewardsEndDate time_text
rows[].deliverDate time_text
rows[].nextSubscriptionDate time_text
rows[].redeemingAmt decimal
rows[].redeemTo text = FLEXIBLE|SPOT
rows[].canRedeemEarly boolean
rows[].autoSubscribe boolean
rows[].type text = AUTO|NORMAL
rows[].status text = HOLDING|REDEEMING|RENEWING|NEW_TRANSFERRING
total integer
""",
    "onchain-redemptions": """
rows[].positionId text
rows[].redeemId id!
rows[].time time_ms!
rows[].asset text!
rows[].lockPeriod text
rows[].amount decimal!
rows[].originalAmount decimal
rows[].type text = NORMAL|EARLY|CONVERT
rows[].deliverDate time_text
rows[].lossAmount decimal
rows[].isComplete boolean
rows[].rewardAsset text
rows[].rewardAmt decimal
rows[].status text = CREATED|PAID
total integer
""",
    "onchain-rewards": """
rows[].positionId text
rows[].time time_ms!
rows[].asset text!
rows[].lockPeriod text
rows[].amount decimal!
total integer
""",
    "onchain-subscriptions": """
rows[].positionId text
rows[].purchaseId text!
rows[].projectId text
rows[].clientId text
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].lockPeriod text
rows[].type text = NORMAL|AUTO
rows[].sourceAccount text = SPOT|FUNDING|SPOTANDFUNDING
rows[].amtFromSpot decimal
rows[].amtFromFunding decimal
rows[].status text = PURCHASING|SUCCESS|FAILED
total integer
""",
    "opt-account": """
asset[].asset text!
asset[].marginBalance decimal
asset[].equity decimal
asset[].available decimal
asset[].initialMargin decimal
asset[].maintMargin decimal
asset[].unrealizedPNL decimal
asset[].adjustedEquity decimal
greek[].underlying text
greek[].delta decimal
greek[].gamma decimal
greek[].theta decimal
greek[].vega decimal
time time_ms
canTrade boolean
canDeposit boolean
canWithdraw boolean
reduceOnly boolean
tradeGroupId id
""",
    "opt-bill": """
[].id id!
[].asset text!
[].amount decimal!
[].symbol text
[].type text
[].createDate time_ms!
""",
    "opt-exercises": """
[].id text!
[].currency text
[].symbol text
[].exercisePrice decimal
[].quantity decimal
[].amount decimal!
[].fee decimal
[].createDate time_ms!
[].priceScale integer
[].quantityScale integer
[].optionSide text
[].positionSide text
[].quoteAsset text
""",
    "opt-positions": """
[].entryPrice decimal
[].symbol text!
[].side text!
[].quantity decimal
[].markValue decimal
[].unrealizedPNL decimal
[].markPrice decimal
[].strikePrice decimal
[].expiryDate time_ms
[].priceScale integer
[].quantityScale integer
[].optionSide text
[].quoteAsset text
[].time time_ms!
[].bidQuantity decimal
[].askQuantity decimal
""",
    "opt-trades": """
[].id id!
[].tradeId id
[].orderId id
[].symbol text
[].price decimal!
[].quantity decimal!
[].fee decimal
[].realizedProfit decimal
[].side text
[].type text
[].liquidity text = TAKER|MAKER
[].time time_ms!
[].priceScale integer
[].quantityScale integer
[].optionSide text
[].quoteAsset text
""",
    "pay": """
code text
message text
data[].orderType text = PAY|PAY_REFUND|C2C|CRYPTO_BOX|CRYPTO_BOX_RF|C2C_HOLDING|
    C2C_HOLDING_RF|PAYOUT|REMITTANCE
data[].transactionId text!
data[].transactionTime time_ms!
data[].amount decimal!
data[].currency text
data[].walletType integer = 1|2|3|4|5|6
data[].walletTypes[] integer
data[].fundsDetail[].currency text
data[].fundsDetail[].amount decimal
data[].fundsDetail[].walletAssetCost object
data[].fundsDetail[].walletAssetCost.<walletType> decimal
data[].payerInfo.name text
data[].payerInfo.type text = USER|MERCHANT
data[].payerInfo.binanceId text
data[].receiverInfo.name text
data[].receiverInfo.type text = USER|MERCHANT
data[].receiverInfo.email text
data[].receiverInfo.binanceId text
data[].receiverInfo.accountId text
data[].receiverInfo.countryCode text
data[].receiverInfo.phoneNumber text
data[].receiverInfo.mobileCode text
data[].receiverInfo.extend.institutionName text
data[].receiverInfo.extend.cardNumber text
data[].receiverInfo.extend.digitalWalletId text
success boolean
""",
    "pm-account": """
accountEquity decimal
accountInitialMargin decimal
accountMaintMargin decimal
accountStatus text = NORMAL|MARGIN_CALL|SUPPLY_MARGIN|REDUCE_ONLY|ACTIVE_LIQUIDATION|
    FORCE_LIQUIDATION|BANKRUPTED
actualEquity decimal
totalAvailableBalance decimal
totalMarginOpenLoss decimal
uniMMR decimal
updateTime time_ms
virtualMaxWithdrawAmount decimal
""",
    "pm-balance": """
[].asset text!
[].cmUnrealizedPNL decimal
[].cmWalletBalance decimal
[].crossMarginAsset decimal
[].crossMarginBorrowed decimal
[].crossMarginFree decimal
[].crossMarginInterest decimal
[].crossMarginLocked decimal
[].negativeBalance decimal
[].totalWalletBalance decimal!
[].umUnrealizedPNL decimal
[].umWalletBalance decimal
[].updateTime time_ms
""",
    "rebate": """
status text
type text
code text
data.page integer
data.totalRecords integer
data.totalPageNum integer
data.data[].asset text!
data.data[].type integer = 1|2
data.data[].amount decimal!
data.data[].updateTime time_ms!
""",
    "rwusd-account": """
rwusdAmount decimal!
totalProfit decimal!
""",
    "rwusd-redemptions": """
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].receiveAsset text!
rows[].receiveAmount decimal!
rows[].fee decimal
rows[].arrivalTime time_ms
rows[].status text = PENDING|SUCCESS
total integer
""",
    "rwusd-rewards": """
rows[].time time_ms!
rows[].rewardsAmount decimal!
rows[].rwusdPosition decimal
rows[].annualPercentageRate decimal
total integer
""",
    "rwusd-subscriptions": """
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].receiveAsset text!
rows[].receiveAmount decimal!
rows[].status text
total integer
""",
    "snapshot": """
code integer
msg text
snapshotVos[].type text!
snapshotVos[].updateTime time_ms!
snapshotVos[].data object
snapshotVos[].data.balances[].asset text
snapshotVos[].data.balances[].free decimal
snapshotVos[].data.balances[].locked decimal
snapshotVos[].data.totalAssetOfBtc decimal
snapshotVos[].data.marginLevel decimal
snapshotVos[].data.totalLiabilityOfBtc decimal
snapshotVos[].data.totalNetAssetOfBtc decimal
snapshotVos[].data.userAssets[].asset text
snapshotVos[].data.userAssets[].borrowed decimal
snapshotVos[].data.userAssets[].free decimal
snapshotVos[].data.userAssets[].interest decimal
snapshotVos[].data.userAssets[].locked decimal
snapshotVos[].data.userAssets[].netAsset decimal
snapshotVos[].data.assets[].asset text
snapshotVos[].data.assets[].marginBalance decimal
snapshotVos[].data.assets[].walletBalance decimal
snapshotVos[].data.position[].entryPrice decimal
snapshotVos[].data.position[].markPrice decimal
snapshotVos[].data.position[].positionAmt decimal
snapshotVos[].data.position[].symbol text
snapshotVos[].data.position[].unRealizedProfit decimal
""",
    "soft-staking-list": """
status boolean
totalRewardsUsdt decimal
rows[].asset text!
rows[].minAmount decimal
rows[].maxCap decimal
rows[].apr decimal
rows[].stakedAmount decimal
rows[].totalProfit decimal
total integer
""",
    "soft-staking-rewards": """
rows[].asset text!
rows[].rewards decimal!
rows[].rewardAsset text
rows[].avgAmount decimal
rows[].time time_ms!
total integer
""",
    "sol-boost-rewards": """
rows[].time time_ms!
rows[].token text!
rows[].amount decimal!
rows[].bnsolHolding decimal
rows[].status text
total integer
""",
    "sol-redemptions": """
rows[].time time_ms!
rows[].arrivalTime time_ms
rows[].asset text!
rows[].amount decimal!
rows[].distributeAsset text!
rows[].distributeAmount decimal!
rows[].exchangeRate decimal
rows[].status text
total integer
""",
    "sol-stakes": """
rows[].time time_ms!
rows[].asset text!
rows[].amount decimal!
rows[].distributeAsset text!
rows[].distributeAmount decimal!
rows[].exchangeRate decimal
rows[].status text
total integer
""",
    "sol-staking-account": """
bnsolAmount decimal!
holdingInSOL decimal!
thirtyDaysProfitInSOL decimal!
""",
    "sol-unclaimed-rewards": """
[].amount decimal!
[].rewardsAsset text!
""",
    "spot-account": """
makerCommission integer
takerCommission integer
buyerCommission integer
sellerCommission integer
commissionRates object
commissionRates.maker decimal
commissionRates.taker decimal
commissionRates.buyer decimal
commissionRates.seller decimal
canTrade boolean
canWithdraw boolean
canDeposit boolean
brokered boolean
requireSelfTradePrevention boolean
preventSor boolean
updateTime time_ms
accountType text
balances[].asset text!
balances[].free decimal!
balances[].locked decimal!
permissions[] text
uid id
""",
    "time": """
serverTime time_ms!
""",
    "travel-deposits": """
[].trId id!
[].tranId id
[].amount decimal!
[].coin text!
[].network text
[].depositStatus integer
[].travelRuleStatus integer = 0|1|2
[].travelRuleStatusV2 text = PASSED|PENDING|REJECTED
[].address text
[].addressTag text
[].txId text
[].insertTime time_ms!
[].completeTime time_ms
[].transferType integer
[].confirmTimes text
[].requireQuestionnaire boolean
[].questionnaire text
""",
    "travel-withdrawals": """
[].id text!
[].trId id
[].amount decimal!
[].transactionFee decimal
[].coin text!
[].withdrawalStatus integer
[].travelRuleStatus integer = 0|1|2
[].address text
[].txId text
[].applyTime time_text!
[].network text
[].transferType integer = 0|1
[].withdrawOrderId text
[].info text
[].confirmNo integer
[].walletType integer = 0|1
[].txKey text
[].questionnaire text
[].completeTime time_text
""",
    "um-account": """
totalInitialMargin decimal
totalMaintMargin decimal
totalWalletBalance decimal
totalUnrealizedProfit decimal
totalMarginBalance decimal
totalPositionInitialMargin decimal
totalOpenOrderInitialMargin decimal
totalCrossWalletBalance decimal
totalCrossUnPnl decimal
availableBalance decimal
maxWithdrawAmount decimal
assets[].asset text!
assets[].walletBalance decimal!
assets[].unrealizedProfit decimal
assets[].marginBalance decimal
assets[].maintMargin decimal
assets[].initialMargin decimal
assets[].positionInitialMargin decimal
assets[].openOrderInitialMargin decimal
assets[].crossWalletBalance decimal
assets[].crossUnPnl decimal
assets[].availableBalance decimal
assets[].maxWithdrawAmount decimal
assets[].updateTime time_ms!
positions[].symbol text
positions[].positionSide text
positions[].positionAmt decimal
positions[].unrealizedProfit decimal
positions[].isolatedMargin decimal
positions[].notional decimal
positions[].isolatedWallet decimal
positions[].initialMargin decimal
positions[].maintMargin decimal
positions[].updateTime time_ms
""",
    "um-balance": """
[].accountAlias text
[].asset text!
[].balance decimal!
[].crossWalletBalance decimal
[].crossUnPnl decimal
[].availableBalance decimal
[].maxWithdrawAmount decimal
[].marginAvailable boolean
[].updateTime time_ms!
""",
    "um-income": """
[].symbol text
[].incomeType text! = TRANSFER|WELCOME_BONUS|REALIZED_PNL|FUNDING_FEE|COMMISSION|
    INSURANCE_CLEAR|REFERRAL_KICKBACK|COMMISSION_REBATE|API_REBATE|CONTEST_REWARD|CROSS_COLLATERAL_TRANSFER|OPTIONS_PREMIUM_FEE|OPTIONS_SETTLE_PROFIT|INTERNAL_TRANSFER|AUTO_EXCHANGE|DELIVERED_SETTELMENT|COIN_SWAP_DEPOSIT|COIN_SWAP_WITHDRAW|POSITION_LIMIT_INCREASE_FEE|STRATEGY_UMFUTURES_TRANSFER|FEE_RETURN|BFUSD_REWARD|SPECIAL_FUNDING_FEE
[].income decimal!
[].asset text!
[].info text
[].time time_ms!
[].tranId id!
[].tradeId text
""",
    "um-positions": """
[].symbol text!
[].positionSide text!
[].positionAmt decimal!
[].entryPrice decimal
[].breakEvenPrice decimal
[].markPrice decimal
[].unRealizedProfit decimal
[].liquidationPrice decimal
[].isolatedMargin decimal
[].notional decimal
[].marginAsset text
[].isolatedWallet decimal
[].initialMargin decimal
[].maintMargin decimal
[].positionInitialMargin decimal
[].openOrderInitialMargin decimal
[].adl integer
[].bidNotional decimal
[].askNotional decimal
[].updateTime time_ms!
""",
    "um-trades": """
[].buyer boolean
[].commission decimal!
[].commissionAsset text!
[].id id!
[].maker boolean
[].orderId id
[].price decimal!
[].qty decimal!
[].quoteQty decimal!
[].baseQty decimal
[].marginAsset text
[].realizedPnl decimal
[].side text
[].positionSide text
[].symbol text!
[].pair text
[].time time_ms!
""",
    "universal-transfer": """
total integer
rows[].asset text!
rows[].amount decimal!
rows[].type text = MAIN_UMFUTURE|MAIN_CMFUTURE|MAIN_MARGIN|UMFUTURE_MAIN|UMFUTURE_MARGIN|
    CMFUTURE_MAIN|CMFUTURE_MARGIN|MARGIN_MAIN|MARGIN_UMFUTURE|MARGIN_CMFUTURE|ISOLATEDMARGIN_MARGIN|MARGIN_ISOLATEDMARGIN|ISOLATEDMARGIN_ISOLATEDMARGIN|MAIN_FUNDING|FUNDING_MAIN|FUNDING_UMFUTURE|UMFUTURE_FUNDING|MARGIN_FUNDING|FUNDING_MARGIN|FUNDING_CMFUTURE|CMFUTURE_FUNDING|MAIN_OPTION|OPTION_MAIN|UMFUTURE_OPTION|OPTION_UMFUTURE|MARGIN_OPTION|OPTION_MARGIN|FUNDING_OPTION|OPTION_FUNDING|MAIN_PORTFOLIO_MARGIN|PORTFOLIO_MARGIN_MAIN
rows[].status text = CONFIRMED|FAILED|PENDING
rows[].tranId id!
rows[].timestamp time_ms!
""",
    "user-asset": """
[].asset text!
[].free decimal!
[].locked decimal!
[].freeze decimal
[].withdrawing decimal
[].ipoable decimal
[].btcValuation decimal
""",
    "vip-loan-collateral-account": """
rows[].collateralAccountId text!
rows[].collateralCoin text!
total integer
""",
    "vip-loan-interest": """
rows[].loanCoin text!
rows[].principalAmount decimal
rows[].interestAmount decimal
rows[].annualInterestRate decimal
rows[].accrualTime time_ms!
rows[].orderId id
total integer
""",
    "vip-loan-ongoing": """
rows[].orderId id!
rows[].loanCoin text!
rows[].totalDebt decimal!
rows[].loanRate decimal
rows[].residualInterest decimal
rows[].collateralAccountId text
rows[].collateralCoin text!
rows[].totalCollateralValueAfterHaircut decimal
rows[].lockedCollateralValue decimal
rows[].currentLTV decimal
rows[].expirationTime time_ms
rows[].loanDate time_text!
rows[].loanTerm text
total integer
""",
    "vip-loan-repays": """
rows[].loanCoin text!
rows[].repayAmount decimal!
rows[].repayPrincipal decimal
rows[].repayInterest decimal
rows[].collateralCoin text!
rows[].repayStatus text = Repaid|Repaying|Failed
rows[].loanDate time_text
rows[].repayTime time_text!
rows[].orderId text
total integer
""",
    "vip-loan-requests": """
rows[].loanAccountId text
rows[].orderId text
rows[].requestId text!
rows[].loanCoin text!
rows[].loanAmount decimal
rows[].collateralAccountId text
rows[].collateralCoin text!
rows[].loanTerm text
rows[].status text = Accruing_Interest|Overdue|Liquidating|Repaying|Repaid|Liquidated|
    Pending|Failed
rows[].loanDate time_text!
total integer
""",
    "wallet-balance": """
[].activate boolean
[].balance decimal!
[].walletName text!
[].assetBalances[] array
[].assetBalances[].asset text
[].assetBalances[].assetName text
[].assetBalances[].free decimal
[].assetBalances[].locked decimal
[].assetBalances[].freeze decimal
[].assetBalances[].withdrawing decimal
[].assetBalances[].btcValuation decimal
""",
    "wbeth-rewards": """
estRewardsInETH decimal
rows[].time time_ms!
rows[].amountInETH decimal
rows[].holding decimal
rows[].holdingInETH decimal
rows[].annualPercentageRate decimal
total integer
""",
    "wbeth-unwraps": """
rows[].time time_ms!
rows[].fromAsset text!
rows[].fromAmount decimal!
rows[].toAsset text!
rows[].toAmount decimal!
rows[].exchangeRate decimal
rows[].status text = PENDING|SUCCESS|FAILED
total integer
""",
    "wbeth-wraps": """
rows[].time time_ms!
rows[].fromAsset text!
rows[].fromAmount decimal!
rows[].toAsset text!
rows[].toAmount decimal!
rows[].exchangeRate decimal
rows[].status text = PENDING|SUCCESS|FAILED
total integer
""",
    "withdrawals": """
[].id text!
[].amount decimal!
[].transactionFee decimal
[].coin text!
[].status integer = 0|1|2|3|4|5|6
[].address text
[].txId text
[].applyTime time_text!
[].network text
[].transferType integer = 0|1
[].withdrawOrderId text
[].info text
[].confirmNo integer
[].walletType integer = 0|1
[].txKey text
[].completeTime time_text
""",
}

FIELDS: dict[str, tuple[Field, ...]] = {
    label: _fields(spec) for label, spec in _FIELD_SPECS.items()
}

# Binance's `walletName` to the key the ledger uses, and the account names `Binance <name>`.
# `Crypto Loans` and `Portfolio Margin` are not names `wallet-balance` returns: the connector
# adds those two accounts itself.
WALLETS: dict[str, str] = {
    "Spot": "spot",
    "Funding": "funding",
    "Earn": "earn",
    "Cross Margin": "cross_margin",
    "Isolated Margin": "isolated_margin",
    "USDⓈ-M Futures": "um_futures",
    "COIN-M Futures": "cm_futures",
    "Options": "options",
    "Trading Bots": "trading_bots",
    "Copy Trading": "copy_trading",
    "Crypto Loans": "loans",
    "Portfolio Margin": "portfolio_margin",
}


def wallet_key(wallet_name: str) -> str:
    """The key of a wallet; one Binance adds later gets `other:<name>`."""
    return WALLETS.get(wallet_name, f"other:{wallet_name}")


def wallet_account_name(wallet_name: str) -> str:
    return f"Binance {wallet_name}"


# The parameter that moves a paged request on, the one that sets its size, and the value the
# cursor starts at. The names differ from product to product.
_CURRENT_SIZE = ("current", "size", 1)
_CURRENT_LIMIT = ("current", "limit", 1)
_PAGING_PARAMS: dict[str, tuple[str, str, int]] = {
    **dict.fromkeys(
        (
            "earn-flexible-position earn-locked-position universal-transfer cloud-mining "
            "earn-flexible-subscriptions earn-flexible-redemptions earn-flexible-rewards "
            "earn-flexible-collateral earn-locked-subscriptions earn-locked-redemptions "
            "earn-locked-rewards bfusd-subscriptions bfusd-redemptions bfusd-rewards "
            "rwusd-subscriptions rwusd-redemptions rwusd-rewards eth-stakes eth-redemptions "
            "wbeth-rewards wbeth-wraps wbeth-unwraps sol-stakes sol-redemptions bnsol-rewards "
            "sol-boost-rewards onchain-locked-position onchain-subscriptions "
            "onchain-redemptions onchain-rewards soft-staking-list soft-staking-rewards "
            "margin-transfers margin-borrow-repay margin-interest margin-liquidations "
            "margin-small-liability margin-liquidation-loan-repays"
        ).split(),
        _CURRENT_SIZE,
    ),
    **dict.fromkeys(
        (
            "loan-flexible-ongoing loan-flexible-borrows loan-flexible-repays "
            "loan-flexible-ltv-adjustments loan-flexible-liquidations vip-loan-ongoing "
            "vip-loan-repays vip-loan-interest vip-loan-collateral-account vip-loan-requests"
        ).split(),
        _CURRENT_LIMIT,
    ),
    **dict.fromkeys("fiat-orders fiat-payments c2c".split(), ("page", "rows", 1)),
    **dict.fromkeys("dci-positions discount-positions".split(), ("pageIndex", "pageSize", 1)),
    # No size parameter: Binance returns up to 200 records a page.
    "rebate": ("page", "", 1),
    **dict.fromkeys(
        "deposits withdrawals travel-deposits travel-withdrawals".split(), ("offset", "limit", 0)
    ),
    **dict.fromkeys(
        "my-trades margin-trades margin-capital-flow opt-trades".split(), ("fromId", "limit", 0)
    ),
    "opt-bill": ("recordId", "limit", 0),
}


def paging_params(endpoint: Endpoint) -> tuple[str, str, int]:
    """The names of an endpoint's cursor and size parameters, and where the cursor starts.

    An endpoint that does not page has no cursor (`""`) and, when it takes one, a `limit`.
    """
    if endpoint.paging == "none":
        return "", "limit" if endpoint.page_size else "", 0
    return _PAGING_PARAMS[endpoint.label]
