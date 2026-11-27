"""Synthetic Binance data and an in-memory Binance, both shaped by the catalogue.

`build_row` and `build_response` make a record and a whole answer for any label, with every
field the catalogue lists. `FakeBinance` answers every label from a store of rows that tests
fill and change: it honours the time window, the paging style and the page size, answers
`/api/v3/time` from an injectable clock and `api-restrictions` from a mutable permissions
dict, can be told to fail, and records what it was asked. Nothing here touches the network,
and every value is invented: assets `AAA`, `BBB`, `STB`, addresses of one repeated digit.
"""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx

from lararium.modules.finance.binance.catalogue import (
    ENDPOINTS,
    FIELDS,
    Endpoint,
    Field,
    paging_params,
)
from lararium.modules.finance.binance.parse import WILDCARD, ApiValue, Node, schema

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
# 2026-11-23 00:00 UTC: the fake's "now" unless a test gives its own clock.
NOW_MS = 1_795_392_000_000
# 2023-11-14 22:13:20 UTC: the time of the first built row.
BASE_TIME_MS = 1_700_000_000_000
BASE_ID = 1_000
ADDRESS = "0x" + "1" * 40
TX_HASH = "0x" + "2" * 64

type Rowdict = dict[str, ApiValue]

# Text fields that hold an asset code.
_ASSET_NAMES = {"currency", "token", "coin", "fiat", "feeAsset"}
# Values the catalogue document records for a field Binance answers with no listed enum: the
# daily snapshot's `type` is lower-case in the answer although the request asks `SPOT`. A
# futures host's exchange information states its request weight per minute as Binance's example
# does (2400).
_RATE_LIMIT: dict[str, ApiValue] = {
    "rateLimits[].rateLimitType": "REQUEST_WEIGHT",
    "rateLimits[].interval": "MINUTE",
    "rateLimits[].intervalNum": 1,
    "rateLimits[].limit": 2400,
}
_RESPONSE_VALUES: dict[str, dict[str, ApiValue]] = {
    "snapshot": {"snapshotVos[].type": "spot"},
    "um-exchange-info": _RATE_LIMIT,
    "cm-exchange-info": _RATE_LIMIT,
    "opt-exchange-info": _RATE_LIMIT,
}
# The futures accounts answer as the probe's did: present, holding nothing (no asset, no
# position) until a test stores rows or sets `positions` (`set_envelope`).
_EMPTY_FUTURES: dict[str, dict[str, ApiValue]] = {
    "um-account": {"positions": []},
    "cm-account": {"positions": []},
    # An options account, once a test opens one (`options`), holds nothing and has no greeks.
    "opt-account": {"greek": []},
}
# Text times written as "YYYY-MM-DD HH:MM:SS" (UTC); every other text time is epoch ms digits.
_UTC_TEXT_LABELS = {"withdrawals", "travel-withdrawals"}


def _name_of(path: str) -> str:
    return path.rpartition(".")[2].strip("[]0123456789") or path


def _time_text(label: str, ms: int) -> str:
    if label in _UTC_TEXT_LABELS:
        return datetime.fromtimestamp(ms // 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return str(ms)


def _text(name: str, seq: int) -> str:
    if name.endswith(("Asset", "Coin")) or name in _ASSET_NAMES or name == "asset":
        return "AAA"
    if name in {"address", "sourceAddress"}:
        return ADDRESS
    if name == "txId" or name == "txKey":
        return TX_HASH
    if name == "symbol" or name.endswith("Symbol"):
        return "AAABBB"
    if name == "walletName":
        return "Spot"
    if name == "network":
        return "NET1"
    if name == "id" or name.endswith(("Id", "No", "Number")):
        return str(BASE_ID + seq)
    return "text"


def _scalar(label: str, spec: Field, seq: int) -> ApiValue:
    if spec.path in _RESPONSE_VALUES.get(label, {}):
        return _RESPONSE_VALUES[label][spec.path]
    if spec.enum is not None:
        first = sorted(spec.enum)[0]
        return int(first) if spec.type in ("integer", "id", "time_ms") else first
    match spec.type:
        case "decimal":
            return f"{1 + seq}.5"
        case "integer":
            return 7
        case "id":
            return BASE_ID + seq
        case "time_ms":
            return BASE_TIME_MS + seq * HOUR_MS
        case "time_text":
            return _time_text(label, BASE_TIME_MS + seq * HOUR_MS)
        case "boolean":
            return True
        case "text":
            return _text(_name_of(spec.path), seq)
        case _:
            raise AssertionError(spec.type)


def _sample(label: str, node: Node, seq: int) -> ApiValue:
    spec = node.field
    if spec is not None and spec.type not in ("object", "array"):
        return _scalar(label, spec, seq)
    if node.positional:
        return [_sample(label, node.children[f"[{i}]"], seq) for i in range(len(node.children))]
    if node.element is not None:
        return [_sample(label, node.element, seq)]
    if spec is not None and spec.type == "array" and not node.is_element:
        return []
    body: Rowdict = {}
    for name, child in node.children.items():
        body["AAA" if name == WILDCARD else name] = _sample(label, child, seq)
    return body


def _rows_node(label: str) -> Node:
    """The node of one record: the body, an array's element, or the element at a path."""
    where = ENDPOINTS[label].rows
    node = schema(label)
    if where == "":
        return node
    if where != ".":
        for step in where.split("."):
            node = node.children[step]
    assert node.element is not None, label
    return node.element


def _as_row(label: str, value: ApiValue) -> Rowdict:
    if isinstance(value, list):  # a bar: position by position
        return {str(i): item for i, item in enumerate(value)}
    assert isinstance(value, dict), label
    return value


def build_row(label: str, seq: int = 0, **overrides: ApiValue) -> Rowdict:
    """A valid synthetic record of the label with every field its catalogue entry lists.

    `seq` makes another record: its ids differ and its times are `seq` hours later. An
    override replaces a top-level field; naming a field the record does not have is an error.
    """
    row = _as_row(label, _sample(label, _rows_node(label), seq))
    for name, value in overrides.items():
        if name not in row:
            raise KeyError(f"{label} has no field {name}")
        row[name] = value
    return row


def _bar(row: Mapping[str, ApiValue]) -> list[ApiValue]:
    return [row[str(i)] for i in range(len(row))]


def build_response(label: str, rows: Iterable[Mapping[str, ApiValue]]) -> ApiValue:
    """The body Binance sends for these records: them in the label's envelope."""
    records = [dict(row) for row in rows]
    where = ENDPOINTS[label].rows
    positional = _rows_node(label).positional
    if where == "":
        if len(records) != 1:
            raise ValueError(f"{label} answers with one record")
        return records[0]
    wrapped: list[ApiValue] = [_bar(r) if positional else r for r in records]
    if where == ".":
        return wrapped
    body = _sample(label, schema(label), 0)
    assert isinstance(body, dict)
    *path, last = where.split(".")
    holder = body
    for step in path:
        inner = holder[step]
        assert isinstance(inner, dict)
        holder = inner
    holder[last] = wrapped
    if "total" in body:
        body["total"] = str(len(records)) if isinstance(body["total"], str) else len(records)
    return body


def _dig(row: Mapping[str, ApiValue], path: str) -> ApiValue:
    value: ApiValue = dict(row)
    for step in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(step)
    return value


def time_ms_of(value: ApiValue) -> int | None:
    """An epoch-millisecond time from a number, digits or "YYYY-MM-DD HH:MM:SS" (UTC)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | Decimal):
        return int(value)
    if value.isdecimal():
        return int(value)
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()) * 1000


PERMISSIONS: dict[str, ApiValue] = {
    **{field.path: False for field in FIELDS["api-restrictions"] if field.type == "boolean"},
    "enableReading": True,
    "ipRestrict": True,
}

# Request parameters a row answers for when it carries the same field with the same value.
_FILTERS = (
    "symbol",
    "asset",
    "coin",
    "loanCoin",
    "collateralCoin",
    "productId",
    "orderId",
    "positionId",
    "purchaseId",
    "redeemId",
    "isolatedSymbol",
)
_DEFAULT_SIZES = {"current": 10, "pageIndex": 10, "page": 100, "fromId": 500}
# The catalogue's my-trades: "The time between startTime and endTime can't be longer than 24
# hours"; a start (or an end) alone asks the 24 hours from (or to) it; a time and `fromId` are
# not a supported combination.
_TIME_SPANS = {"my-trades": DAY_MS, "margin-trades": DAY_MS}
# A margin trade request asks cross margin's trades, or an isolated pair's with
# `isIsolated=TRUE` (the default is FALSE): a row answers the one its `isIsolated` says.
_ISOLATED_FLAG = "isIsolated"
# What an account without margin answers (the probe's account): the cross margin account
# `created: false`, the isolated one no pair, and these -3003 "Margin account does not exist".
_NO_MARGIN = frozenset(
    {
        "margin-trades",
        "margin-transfers",
        "margin-borrow-repay",
        "margin-interest",
        "margin-capital-flow",
        "margin-liquidations",
        "margin-small-liability",
        "margin-liquidation-loan",
        "margin-liquidation-loan-repays",
    }
)
_NO_MARGIN_CODE = -3003
# What an account without options answers on the options host's signed endpoints: the docs give
# no answer for it; their error-code page's -5002 USER_NOT_ACCESS ("Option user not access") is
# what the connector takes as not present there.
_NO_OPTIONS_CODE = -5002


@dataclass(frozen=True)
class Seen:
    """A request the fake received: the label and its own parameters (never the key,
    `timestamp`, `recvWindow` or `signature`)."""

    label: str
    method: str
    params: Mapping[str, str]


@dataclass
class _Failure:
    label: str
    status: int
    code: int | None
    older_than_ms: int | None
    times: int | None
    retry_after: int | None
    when: Mapping[str, str]


@dataclass(frozen=True)
class _Stored:
    row: Rowdict
    when: Mapping[str, str]


# What `FakeBinance.when_asked` runs: told how many earlier asks there were.
type Hook = Callable[[int], Awaitable[None] | None]


class FakeBinance:
    """An in-memory Binance for the catalogue's labels.

    Fill it with `add`/`set_rows` (a row can answer only for requests carrying given
    parameters, for the products with a `type` or `transactionType` request parameter); change
    `permissions` to change what `api-restrictions` says; `fail` makes a label answer with an
    error. A label with one record and nothing stored answers with a default record; one with
    many answers with none. The rows come back oldest first, cut to the request's window and
    page. `now` is the clock `/api/v3/time` reads. Give `secret` and a request with the wrong
    HMAC signature is refused.

    By default the account has no margin, as the probe's: the cross margin account answers
    `created: false` and the margin histories -3003. `margin=True` (or setting `margin`) opens
    it: then they answer from what is stored, as every other label does. The futures wallets
    are there and empty, as the probe's were: a test that needs them stores their rows. The
    account has no options: every signed options request answers -5002 (`_NO_OPTIONS_CODE`);
    `options=True` (or setting `options`) opens it, empty.
    """

    def __init__(
        self,
        *,
        now: Callable[[], int] = lambda: NOW_MS,
        secret: str | None = None,
        margin: bool = False,
        options: bool = False,
    ) -> None:
        self.now = now
        self.margin = margin
        self.options = options
        self.permissions: dict[str, ApiValue] = dict(PERMISSIONS)
        self.requests: list[Seen] = []
        self._secret = secret
        self._stored: dict[str, list[_Stored]] = {}
        self._failures: list[_Failure] = []
        self._hooks: dict[str, Hook] = {}
        self._caps: dict[str, int] = {}
        self._envelopes: dict[str, dict[str, ApiValue]] = {
            label: dict(fields) for label, fields in _EMPTY_FUTURES.items()
        }
        self._routes = {(e.method, e.host, e.path): e for e in ENDPOINTS.values()}

    # What it holds.

    def add(self, label: str, *rows: Rowdict, when: Mapping[str, str] | None = None) -> None:
        """Store rows; with `when`, they answer only requests with those parameters."""
        assert label in ENDPOINTS, label
        self._stored.setdefault(label, []).extend(_Stored(dict(r), when or {}) for r in rows)

    def set_rows(
        self, label: str, rows: Iterable[Rowdict], when: Mapping[str, str] | None = None
    ) -> None:
        self._stored[label] = []
        self.add(label, *rows, when=when)

    def set_envelope(self, label: str, **fields: ApiValue) -> None:
        """Answer the label with these top-level fields beside its records (a margin account
        that says `created: false`)."""
        self._envelopes.setdefault(label, {}).update(fields)

    def rows(self, label: str) -> list[Rowdict]:
        return [stored.row for stored in self._stored.get(label, [])]

    def cap_answers(self, label: str, rows: int) -> None:
        """Answer at most `rows` records of a label that cannot page, however many the request
        asks, saying there is more (`moreData`) when more remain: a cap below the documented
        limit."""
        assert ENDPOINTS[label].paging == "none", label
        self._caps[label] = rows

    # What it was asked.

    def calls(self, label: str) -> list[Seen]:
        return [seen for seen in self.requests if seen.label == label]

    # How it fails.

    def fail(
        self,
        label: str,
        *,
        status: int = 400,
        code: int | None = None,
        older_than_ms: int | None = None,
        times: int | None = None,
        retry_after: int | None = None,
        when: Mapping[str, str] | None = None,
    ) -> None:
        """Answer `label` with an error: always, or `times` times, or only for a request whose
        start time is before `older_than_ms`, or only for one with the parameters `when`."""
        self._failures.append(
            _Failure(label, status, code, older_than_ms, times, retry_after, when or {})
        )

    def clear_failures(self) -> None:
        self._failures.clear()

    # What happens while it is asked.

    def when_asked(self, label: str, callback: Hook | None) -> None:
        """Run `callback(n)` each time `label` is asked, before it is answered; `n` counts the
        earlier asks. A test moves money or the clock there, between a request and its answer.
        A callback that returns an awaitable (one that writes to the database) is awaited
        before the answer, on an async client. None removes it."""
        if callback is None:
            self._hooks.pop(label, None)
        else:
            self._hooks[label] = callback

    # The HTTP side.

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), follow_redirects=True)

    def handler(self, request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        route = (request.method, request.url.host, request.url.path)
        assert request.url.scheme == "https", "the client must use https"
        assert route in self._routes, f"a request outside the catalogue: {route}"
        endpoint = self._routes[route]
        params = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        refused = self._check_signature(request, endpoint, params)
        own = {
            name: value
            for name, value in params.items()
            if name not in {"recvWindow", "timestamp", "signature"}
        }
        self.requests.append(Seen(endpoint.label, request.method, own))
        if refused is not None:
            return refused
        if (hook := self._hooks.get(endpoint.label)) is not None:
            pending = hook(len(self.calls(endpoint.label)) - 1)
            if pending is not None:

                async def after() -> httpx.Response:
                    await pending
                    return self._respond(endpoint, own)

                return after()
        return self._respond(endpoint, own)

    def _respond(self, endpoint: Endpoint, own: Mapping[str, str]) -> httpx.Response:
        failure = self._failure_for(endpoint, own)
        if failure is not None:
            return failure
        if not self.margin and endpoint.label in _NO_MARGIN:
            return _error(400, _NO_MARGIN_CODE)
        if not self.options and endpoint.limit == "eapi" and endpoint.signed:
            return _error(400, _NO_OPTIONS_CODE)
        return self._answer(endpoint, own)

    def _check_signature(
        self, request: httpx.Request, endpoint: Endpoint, params: dict[str, str]
    ) -> httpx.Response | None:
        if not endpoint.signed:
            assert "signature" not in params and "X-MBX-APIKEY" not in request.headers
            return None
        assert request.headers.get("X-MBX-APIKEY"), "a signed request carries the key"
        assert {"timestamp", "recvWindow", "signature"} <= params.keys()
        if self._secret is not None:
            signed, _, signature = request.url.query.decode().rpartition("&signature=")
            expected = hmac.new(self._secret.encode(), signed.encode(), hashlib.sha256)
            if signature != expected.hexdigest():
                return _error(400, -1022)
        return None

    def _failure_for(self, endpoint: Endpoint, own: Mapping[str, str]) -> httpx.Response | None:
        start = own.get(endpoint.time_params[0]) if endpoint.time_params else None
        for failure in self._failures:
            if failure.label != endpoint.label:
                continue
            if any(own.get(name) != value for name, value in failure.when.items()):
                continue
            if failure.older_than_ms is not None and (
                start is None or int(start) >= failure.older_than_ms
            ):
                continue
            if failure.times is not None:
                failure.times -= 1
                if failure.times <= 0:
                    self._failures.remove(failure)
            return _error(failure.status, failure.code, failure.retry_after)
        return None

    # The answers.

    def _answer(self, endpoint: Endpoint, own: Mapping[str, str]) -> httpx.Response:
        label = endpoint.label
        if label == "time":
            return _json({"serverTime": self.now()})
        if label == "api-restrictions":
            return _json({**build_row(label), **self.permissions})
        # A parameter the catalogue always sends is one Binance refuses to go without.
        if any(name not in own for name, _ in endpoint.variants):
            return _error(400, -1102)
        # A time range sent to an endpoint whose documents give it none: Binance's code for a
        # parameter it did not read.
        if endpoint.time_params is None and {"startTime", "endTime"} & set(own):
            return _error(400, -1104)
        if too_wide := self._too_wide(endpoint, own):
            return too_wide
        own, refused = self._spanned(endpoint, own)
        if refused is not None:
            return refused
        chosen = self._select(endpoint, own)
        if not chosen and endpoint.rows == "" and not self._stored.get(label):
            return _json(build_response(label, [build_row(label)]))
        page, total, more = self._page(endpoint, own, chosen, self._caps.get(label))
        body = build_response(label, page)
        if isinstance(body, dict):
            self._patch_envelope(body, total, more)
            if label == "margin-account" and not self.margin:
                body["created"] = False
            body.update(self._envelopes.get(label, {}))
        return _json(body)

    @staticmethod
    def _too_wide(endpoint: Endpoint, own: Mapping[str, str]) -> httpx.Response | None:
        if endpoint.time_params is None or endpoint.window_days is None:
            return None
        low, high = endpoint.time_params
        if low not in own or high not in own:
            return None
        if int(own[high]) - int(own[low]) >= (endpoint.window_days + 1) * DAY_MS:
            return _error(400, -1127)
        return None

    @staticmethod
    def _spanned(
        endpoint: Endpoint, own: Mapping[str, str]
    ) -> tuple[Mapping[str, str], httpx.Response | None]:
        """The request's time range as Binance reads it for a label with a fixed longest span:
        refused when wider or combined with `fromId`, completed when one end is missing."""
        span = _TIME_SPANS.get(endpoint.label)
        if span is None or endpoint.time_params is None:
            return own, None
        low, high = endpoint.time_params
        if low not in own and high not in own:
            return own, None
        if "fromId" in own:
            return own, _error(400, -1128)
        if low in own and high in own:
            wide = int(own[high]) - int(own[low]) > span
            return own, _error(400, -1127) if wide else None
        if low in own:
            return {**own, high: str(int(own[low]) + span)}, None
        return {**own, low: str(int(own[high]) - span)}, None

    def _select(self, endpoint: Endpoint, own: Mapping[str, str]) -> list[Rowdict]:
        rows = [
            stored.row
            for stored in self._stored.get(endpoint.label, [])
            if all(own.get(name) == value for name, value in stored.when.items())
        ]
        for name in _FILTERS:
            if name in own:
                rows = [row for row in rows if name not in row or str(row[name]) == own[name]]
        if endpoint.label == "margin-trades":
            isolated = own.get(_ISOLATED_FLAG, "FALSE") == "TRUE"
            rows = [row for row in rows if row.get(_ISOLATED_FLAG) in (None, isolated)]
        if endpoint.time_field is not None and endpoint.time_params is not None:
            low, high = endpoint.time_params
            field = endpoint.time_field

            def inside(row: Rowdict) -> bool:
                when = time_ms_of(_dig(row, field))
                if when is None:
                    return True
                return (low not in own or when >= int(own[low])) and (
                    high not in own or when <= int(own[high])
                )

            rows = [row for row in rows if inside(row)]
        return sorted(rows, key=lambda row: self._order(endpoint, row))

    @staticmethod
    def _order(endpoint: Endpoint, row: Rowdict) -> tuple[int, str]:
        when = time_ms_of(_dig(row, endpoint.time_field)) if endpoint.time_field else None
        return when or 0, json.dumps([_dig(row, name) for name in endpoint.id_fields], default=str)

    @staticmethod
    def _page(
        endpoint: Endpoint, own: Mapping[str, str], rows: list[Rowdict], cap_to: int | None
    ) -> tuple[list[Rowdict], int, bool]:
        """The rows of the asked page, how many there were in all, and whether more remain."""
        cursor, size_name, first = paging_params(endpoint)
        cap = endpoint.page_size or len(rows) or 1
        size = int(own[size_name]) if size_name in own else _DEFAULT_SIZES.get(cursor, cap)
        if size_name == "" and cursor:
            size = cap
        size = min(size, cap) if endpoint.page_size else size
        if endpoint.paging == "none":
            size = min(size, cap_to) if cap_to is not None else size
            return rows[:size], len(rows), len(rows) > size
        if endpoint.paging == "offset":
            start = int(own.get(cursor, first))
            return rows[start : start + size], len(rows), len(rows) > start + size
        if endpoint.paging == "page":
            start = (int(own.get(cursor, first)) - first) * size
            return rows[start : start + size], len(rows), len(rows) > start + size
        key = endpoint.id_fields[-1]
        after = int(own.get(cursor, 0))
        strict = endpoint.label == "margin-capital-flow"
        rest = [
            r for r in rows if (int(str(r[key])) > after if strict else int(str(r[key])) >= after)
        ]
        rest.sort(key=lambda r: int(str(r[key])))
        return rest[:size], len(rest), len(rest) > size

    @staticmethod
    def _patch_envelope(body: dict[str, ApiValue], total: int, more: bool) -> None:
        if "total" in body:
            body["total"] = str(total) if isinstance(body["total"], str) else total
        if "moreData" in body:
            body["moreData"] = more
        data = body.get("data")
        if isinstance(data, dict) and "totalRecords" in data:
            data["totalRecords"] = total


def _json(body: ApiValue) -> httpx.Response:
    def plain(value: ApiValue) -> object:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, list):
            return [plain(item) for item in value]
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        return value

    return httpx.Response(200, content=json.dumps(plain(body)).encode())


def _error(status: int, code: int | None, retry_after: int | None = None) -> httpx.Response:
    content = json.dumps({"code": code, "msg": "synthetic"}).encode() if code is not None else b""
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return httpx.Response(status, content=content, headers=headers)
