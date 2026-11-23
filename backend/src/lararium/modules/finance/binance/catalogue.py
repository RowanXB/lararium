"""The Binance read-only catalogue: every request M7 may send, one `Endpoint` per label.

The client's allowlist is computed from `ENDPOINTS`, so an endpoint that is not listed here
can never be sent. Task 2 fills in the remaining labels and the `FIELDS`.
"""

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


@dataclass(frozen=True)
class Endpoint:
    label: str
    method: Literal["GET", "POST"]
    host: str
    path: str
    weight: int
    limit: Literal["api", "fapi", "dapi", "eapi", "papi", "ip", "uid"]
    signed: bool
    rows: str
    id_fields: tuple[str, ...]
    time_field: str | None
    window_days: int | None
    lookback_days: int | None
    time_params: tuple[str, str]
    paging: Literal["none", "offset", "page", "from_id"]
    page_size: int
    variants: tuple[tuple[str, tuple[str, ...]], ...]
    kind: Literal["public", "account", "balance", "position", "history"]
    phase: Literal["A", "B"]


ENDPOINTS: dict[str, Endpoint] = {
    "time": Endpoint(
        label="time",
        method="GET",
        host="api.binance.com",
        path="/api/v3/time",
        weight=1,
        limit="api",
        signed=False,
        rows="",
        id_fields=(),
        time_field="serverTime",
        window_days=None,
        lookback_days=None,
        time_params=("startTime", "endTime"),
        paging="none",
        page_size=0,
        variants=(),
        kind="public",
        phase="A",
    ),
    "api-restrictions": Endpoint(
        label="api-restrictions",
        method="GET",
        host="api.binance.com",
        path="/sapi/v1/account/apiRestrictions",
        weight=1,
        limit="ip",
        signed=True,
        rows="",
        id_fields=(),
        time_field="createTime",
        window_days=None,
        lookback_days=None,
        time_params=("startTime", "endTime"),
        paging="none",
        page_size=0,
        variants=(),
        kind="account",
        phase="A",
    ),
    "spot-account": Endpoint(
        label="spot-account",
        method="GET",
        host="api.binance.com",
        path="/api/v3/account",
        weight=20,
        limit="api",
        signed=True,
        rows="balances",
        id_fields=("asset",),
        time_field="updateTime",
        window_days=None,
        lookback_days=None,
        time_params=("startTime", "endTime"),
        paging="none",
        page_size=0,
        variants=(),
        kind="balance",
        phase="A",
    ),
    "funding-asset": Endpoint(
        label="funding-asset",
        method="POST",
        host="api.binance.com",
        path="/sapi/v1/asset/get-funding-asset",
        weight=1,
        limit="ip",
        signed=True,
        rows="",
        id_fields=("asset",),
        time_field=None,
        window_days=None,
        lookback_days=None,
        time_params=("startTime", "endTime"),
        paging="none",
        page_size=0,
        variants=(),
        kind="balance",
        phase="A",
    ),
    "user-asset": Endpoint(
        label="user-asset",
        method="POST",
        host="api.binance.com",
        path="/sapi/v3/asset/getUserAsset",
        weight=5,
        limit="ip",
        signed=True,
        rows="",
        id_fields=("asset",),
        time_field=None,
        window_days=None,
        lookback_days=None,
        time_params=("startTime", "endTime"),
        paging="none",
        page_size=0,
        variants=(),
        kind="balance",
        phase="A",
    ),
    "withdrawals": Endpoint(
        label="withdrawals",
        method="GET",
        host="api.binance.com",
        path="/sapi/v1/capital/withdraw/history",
        weight=18_000,
        limit="uid",
        signed=True,
        rows="",
        id_fields=("id",),
        time_field="applyTime",
        window_days=89,
        lookback_days=None,
        time_params=("startTime", "endTime"),
        paging="offset",
        page_size=1000,
        variants=(),
        kind="history",
        phase="A",
    ),
}
