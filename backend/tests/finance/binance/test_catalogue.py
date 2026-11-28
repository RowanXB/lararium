import pytest

from lararium.modules.finance.binance.catalogue import (
    ENDPOINTS,
    FIELDS,
    WALLETS,
    Endpoint,
    paging_params,
    wallet_account_name,
    wallet_key,
)
from tests.finance.binance import catalogue_doc as doc

PHASE_A = {
    "time",
    "klines",
    "exchange-info",
    "api-restrictions",
    "account-info",
    "account-status",
    "api-trading-status",
    "spot-account",
    "user-asset",
    "funding-asset",
    "wallet-balance",
    "earn-account",
    "earn-flexible-position",
    "snapshot",
    "coin-config",
    "asset-detail",
    "deposits",
    "withdrawals",
    "convert",
    "dividends",
    "earn-flexible-subscriptions",
    "earn-flexible-redemptions",
    "earn-flexible-rewards",
    "my-trades",
}


def field_paths(label: str) -> set[str]:
    return {spec.path for spec in FIELDS[label]}


def row_prefix(endpoint: Endpoint) -> str:
    return {"": "", ".": "[]."}.get(endpoint.rows, endpoint.rows + "[].")


def test_catalogue_matches_the_document() -> None:
    lines = doc.read()
    index = doc.index(lines)
    problems: list[str] = []
    if set(ENDPOINTS) != set(index):
        problems.append(f"labels differ: {sorted(set(ENDPOINTS) ^ set(index))}")
    if set(FIELDS) != set(index):
        problems.append(f"labels with fields differ: {sorted(set(FIELDS) ^ set(index))}")
    for label, row in index.items():
        endpoint = ENDPOINTS.get(label)
        if endpoint is None:
            continue
        if (endpoint.method, endpoint.host, endpoint.path) != (row.method, row.host, row.path):
            problems.append(f"{label}: method, host or path differ")
        coded = [spec.path for spec in FIELDS.get(label, ())]
        if len(coded) != len(set(coded)):
            problems.append(f"{label}: a field is listed twice")
        if row.fields == "own heading":
            stated = set(doc.paths_of(doc.field_rows(lines, label)))
        else:
            # The two Portfolio Margin labels: section 12 names their fields in prose.
            names = doc.portfolio_margin_names(lines, row.path)
            stated = {name.removeprefix("[].") for name in coded}
            assert stated == names, f"{label}: {stated ^ names}"
            continue
        if set(coded) != stated:
            problems.append(
                f"{label}: only in code {sorted(set(coded) - stated)}, "
                f"only in the document {sorted(stated - set(coded))}"
            )
    assert problems == []


@pytest.mark.parametrize(
    ("cell", "paths"),
    [
        ("serverTime", ["serverTime"]),
        ("a, b, c", ["a", "b", "c"]),
        ("commissionRates.maker, .taker", ["commissionRates.maker", "commissionRates.taker"]),
        ("rows[].x, y", ["rows[].x", "rows[].y"]),
        ("rows[].a / b", ["rows[].a", "rows[].b"]),
        ("SPOT: snapshotVos[].data.x", ["snapshotVos[].data.x"]),
        ("SPOT + MARGIN: data.totalAssetOfBtc", ["data.totalAssetOfBtc"]),
        ("MARGIN: d.rows[].a, .b", ["d.rows[].a", "d.rows[].b"]),
        ("…filters[] PRICE_FILTER", []),
    ],
)
def test_the_document_reader_expands_a_first_column_cell(cell: str, paths: list[str]) -> None:
    assert doc.expand(cell) == paths


def test_the_document_reader_copies_a_sibling_object() -> None:
    rows = [
        doc.DocField("assets[].baseAsset", "object", ""),
        doc.DocField("assets[].baseAsset.asset", "string", ""),
        doc.DocField("assets[].quoteAsset.*", "—", "same 1 field as `baseAsset`"),
        doc.DocField("assets[].symbol", "string", ""),
    ]
    assert doc.paths_of(rows) == [
        "assets[].baseAsset",
        "assets[].baseAsset.asset",
        "assets[].quoteAsset",
        "assets[].quoteAsset.asset",
        "assets[].symbol",
    ]


def test_only_two_posts() -> None:
    assert {e.label for e in ENDPOINTS.values() if e.method == "POST"} == {
        "funding-asset",
        "user-asset",
    }


def test_real_names_win() -> None:
    for label in ("earn-flexible-redemptions", "earn-flexible-rewards"):
        assert "rows[].productId" in field_paths(label)
        assert "rows[].projectId" not in field_paths(label)
    assert {"list[].orderType", "list[].side", "list[].walletType"} <= field_paths("convert")
    assert "[].sourceAddress" in field_paths("deposits")
    assert "transferEnabled" in field_paths("margin-account")
    assert "positions[].isolatedWallet" in field_paths("cm-account")
    assert {"[].isolatedWallet", "[].notionalValue"} <= field_paths("cm-positions")


def test_the_phase_a_labels_are_the_ones_the_plan_names() -> None:
    assert {e.label for e in ENDPOINTS.values() if e.phase == "A"} == PHASE_A


def test_only_the_public_reads_are_unsigned() -> None:
    assert {e.label for e in ENDPOINTS.values() if not e.signed} == {
        "time",
        "exchange-info",
        "klines",
        # Each futures host's own, for its rate limits (plan Decision 4).
        "um-exchange-info",
        "cm-exchange-info",
        # The options host's, likewise.
        "opt-exchange-info",
    }


def test_each_endpoint_uses_the_host_its_limit_names() -> None:
    for endpoint in ENDPOINTS.values():
        expected = (
            f"{endpoint.limit}.binance.com"
            if endpoint.limit in {"fapi", "dapi", "eapi", "papi"}
            else "api.binance.com"
        )
        assert endpoint.host == expected, endpoint.label


def test_the_columns_of_every_endpoint_point_at_fields_of_its_label() -> None:
    for endpoint in ENDPOINTS.values():
        label = endpoint.label
        paths = field_paths(label)
        prefix = row_prefix(endpoint)
        if endpoint.rows not in ("", "."):
            # The array of records is named by its elements' fields.
            assert any(path.startswith(prefix) for path in paths), label
        for name in (*endpoint.id_fields, *([endpoint.time_field] if endpoint.time_field else [])):
            wanted = prefix + name if prefix else name
            if label == "klines":
                wanted = f"[][{name}]"
            assert wanted in paths, f"{label}: {name} is not a field of its rows"
        assert endpoint.weight > 0
        assert endpoint.window_days is None or endpoint.window_days > 0
        assert endpoint.lookback_days is None or endpoint.lookback_days > 0
        assert all(values for _, values in endpoint.variants), label


def test_every_paged_endpoint_names_its_parameters() -> None:
    for endpoint in ENDPOINTS.values():
        cursor, size, first = paging_params(endpoint)
        if endpoint.paging == "none":
            assert (cursor, first) == ("", 0)
            assert size == ("limit" if endpoint.page_size else "")
        else:
            assert cursor and first in (0, 1), endpoint.label
            assert endpoint.page_size > 0, endpoint.label


def test_time_params_are_the_documented_names() -> None:
    odd = {
        e.label: e.time_params
        for e in ENDPOINTS.values()
        if e.time_params != ("startTime", "endTime")
    }
    assert odd == {
        "fiat-orders": ("beginTime", "endTime"),
        "fiat-payments": ("beginTime", "endTime"),
        "c2c": ("startTimestamp", "endTimestamp"),
        # The documents give it paging only.
        "vip-loan-requests": None,
        # The futures balances, positions and exchange information take no time range.
        **{
            f"{host}-{name}": None
            for host in ("um", "cm")
            for name in ("account", "balance", "positions", "exchange-info")
        },
        # The options account, positions and exchange information likewise.
        **{f"opt-{name}": None for name in ("account", "positions", "exchange-info")},
    }


def test_the_variants_of_a_transfer_are_documented_types() -> None:
    (name, types), *rest = ENDPOINTS["universal-transfer"].variants
    documented = next(f.enum for f in FIELDS["universal-transfer"] if f.path == "rows[].type")
    assert rest == [] and name == "type"
    assert documented is not None and set(types) <= documented
    # The three isolated-margin types need a symbol, so they are not asked for blindly.
    assert documented - set(types) == {
        "ISOLATEDMARGIN_MARGIN",
        "MARGIN_ISOLATEDMARGIN",
        "ISOLATEDMARGIN_ISOLATEDMARGIN",
    }


def test_wallets() -> None:
    assert WALLETS["Spot"] == "spot"
    assert WALLETS["USDⓈ-M Futures"] == "um_futures"
    assert WALLETS["Crypto Loans"] == "loans"
    assert WALLETS["Portfolio Margin"] == "portfolio_margin"
    assert len(WALLETS) == len(set(WALLETS.values())) == 12
    assert wallet_key("Cross Margin") == "cross_margin"
    assert wallet_key("Something New") == "other:Something New"
    assert wallet_account_name("Crypto Loans") == "Binance Crypto Loans"
