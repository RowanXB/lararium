"""The Esplora client: paced, fail-closed parsing, no address in any error."""

import itertools
from decimal import Decimal
from typing import Any

import httpx
import pytest

from lararium.core.errors import DisplayableError
from lararium.modules.finance.bitcoin.client import EsploraClient
from lararium.modules.finance.bitcoin.parse import (
    MalformedResponse,
    parse_stats,
    parse_txs,
    unknown_fields,
)
from lararium.modules.finance.readonly_http import loads_decimal
from tests.finance.bitcoin import fixtures as fx

BASE = "https://esplora.example.test/api"


class Env:
    """A fake clock that sleeping advances, and a transport that records each request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.now = Decimal(1000)
        self.sleeps: list[int] = []
        self.sent: list[tuple[Decimal, str]] = []
        self._responses = list(responses)

    async def sleep(self, seconds: int) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> Decimal:
        return self.now

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((self.now, request.url.path))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def client(self) -> EsploraClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return EsploraClient(http, BASE, sleep=self.sleep, clock=self.clock)


def ok(value: Any) -> httpx.Response:
    return httpx.Response(200, text=fx.body(value))


def text(raw: str) -> httpx.Response:
    return httpx.Response(200, text=raw)


async def test_paces_one_request_a_second() -> None:
    env = Env(ok(fx.stats()))
    client = env.client()
    for _ in range(5):
        await client.stats(fx.RECEIVING_0)
    times = [at for at, _ in env.sent]
    assert len(times) == 5
    assert all(later - earlier >= 1 for earlier, later in itertools.pairwise(times))
    assert env.sleeps == [1, 1, 1, 1]


async def test_429_without_retry_after_waits_60() -> None:
    env = Env(httpx.Response(429), ok(fx.stats()))
    await env.client().stats(fx.RECEIVING_0)
    assert 60 in env.sleeps


async def test_stats_returns_parsed_and_raw() -> None:
    env = Env(ok(fx.stats(tx_count=2, funded_txo_sum=70_000, spent_txo_sum=10_000)))
    parsed, raw = await env.client().stats(fx.RECEIVING_0)
    assert parsed.address == fx.RECEIVING_0
    assert parsed.chain.funded_txo_sum == 70_000
    assert parsed.mempool.tx_count == 0
    assert isinstance(raw, dict)
    assert env.sent[0][1] == f"/api/address/{fx.RECEIVING_0}"


async def test_tip_height() -> None:
    env = Env(text("840000"))
    assert await env.client().tip_height() == 840_000
    assert env.sent[0][1] == "/api/blocks/tip/height"


async def test_history_pages_by_last_seen_txid() -> None:
    env = Env(ok([fx.tx(fx.TXID_A)]), ok([fx.tx(fx.TXID_B)]))
    client = env.client()
    first, _ = await client.txs(fx.RECEIVING_0)
    second, raw = await client.txs(fx.RECEIVING_0, after=first[-1].txid)
    assert [path for _, path in env.sent] == [
        f"/api/address/{fx.RECEIVING_0}/txs",
        f"/api/address/{fx.RECEIVING_0}/txs/chain/{fx.TXID_A}",
    ]
    assert second[0].txid == fx.TXID_B
    assert isinstance(raw, list)


async def test_txs_keep_every_documented_field() -> None:
    env = Env(ok([fx.tx(), fx.coinbase_tx(), fx.tx(fx.TXID_B, confirmed=False)]))
    txs, _ = await env.client().txs(fx.RECEIVING_0)
    first, coinbase, pending = txs
    assert (first.fee, first.sigops, first.vin[0].prevout.value) == (141, 1, 100_000)  # pyright: ignore[reportOptionalMemberAccess]
    assert first.vin[0].witness == ("30" * 36, "02" * 33)
    assert first.vin[0].inner_redeemscript_asm is None
    assert first.vout[0].scriptpubkey_address == fx.RECEIVING_0
    assert first.status.block_time is not None
    assert first.status.block_time.tzinfo is not None
    assert coinbase.vin[0].prevout is None and coinbase.vin[0].witness == ()
    assert pending.status.confirmed is False and pending.status.block_height is None


async def test_missing_prevout_is_malformed() -> None:
    bad = fx.without(fx.tx(), "vin", 0, "prevout")
    with pytest.raises(MalformedResponse):
        await Env(ok([bad])).client().txs(fx.RECEIVING_0)


async def test_missing_fee_is_malformed() -> None:
    with pytest.raises(MalformedResponse):
        await Env(ok([fx.without(fx.tx(), "fee")])).client().txs(fx.RECEIVING_0)


@pytest.mark.parametrize("amount", ["5.0", "5.5", "5e3"])
async def test_float_amount_is_malformed(amount: str) -> None:
    raw = fx.body([fx.tx()]).replace('"value": 60000', f'"value": {amount}')
    with pytest.raises(MalformedResponse):
        await Env(text(raw)).client().txs(fx.RECEIVING_0)


async def test_float_stat_is_malformed() -> None:
    raw = fx.body(fx.stats(funded_txo_sum=1)).replace(
        '"funded_txo_sum": 1', '"funded_txo_sum": 1.5'
    )
    with pytest.raises(MalformedResponse):
        await Env(text(raw)).client().stats(fx.RECEIVING_0)


async def test_negative_amount_is_malformed() -> None:
    bad = fx.tx()
    bad["vout"][0]["value"] = -1
    with pytest.raises(MalformedResponse):
        await Env(ok([bad])).client().txs(fx.RECEIVING_0)


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "{}", '{"chain_stats": 1}'])
async def test_stats_of_the_wrong_shape_is_malformed(raw: str) -> None:
    with pytest.raises(MalformedResponse):
        await Env(text(raw)).client().stats(fx.RECEIVING_0)


async def test_txs_must_be_a_list() -> None:
    with pytest.raises(MalformedResponse):
        await Env(ok(fx.tx())).client().txs(fx.RECEIVING_0)


@pytest.mark.parametrize("raw", ['"840000"', "840000.5", "-1", "null", "true"])
async def test_tip_height_of_the_wrong_shape_is_malformed(raw: str) -> None:
    with pytest.raises(MalformedResponse):
        await Env(text(raw)).client().tip_height()


async def test_unconfirmed_status_with_a_block_is_malformed() -> None:
    bad = fx.tx(confirmed=False)
    bad["status"] = {"confirmed": False, "block_height": 5}
    with pytest.raises(MalformedResponse):
        await Env(ok([bad])).client().txs(fx.RECEIVING_0)


async def test_confirmed_status_without_a_block_is_malformed() -> None:
    with pytest.raises(MalformedResponse):
        await Env(ok({"confirmed": True})).client().status(fx.TXID_A)


def test_unknown_field_is_reported() -> None:
    body = [fx.tx()]
    body[0]["vout"][0]["foo"] = 1
    body[0]["bar"] = 2
    parse_txs(loads_decimal(fx.body(body)))
    assert unknown_fields(loads_decimal(fx.body(body))) == {"vout[].foo", "bar"}
    assert unknown_fields(loads_decimal(fx.body([fx.tx(), fx.coinbase_tx()]))) == set()
    assert unknown_fields(loads_decimal(fx.body(fx.stats()))) == set()


async def test_status_by_txid_without_block_time() -> None:
    env = Env(ok({"confirmed": True, "block_height": 5, "block_hash": fx.BLOCK_HASH}))
    parsed, raw = await env.client().status(fx.TXID_A)
    assert parsed is not None and parsed.block_height == 5 and parsed.block_time is None
    assert isinstance(raw, dict)
    assert env.sent[0][1] == f"/api/tx/{fx.TXID_A}/status"


async def test_404_status_is_none() -> None:
    parsed, raw = await Env(httpx.Response(404)).client().status(fx.TXID_A)
    assert parsed is None
    assert raw is None


async def test_errors_never_carry_the_address() -> None:
    address = fx.RECEIVING_0
    failures = [
        (httpx.Response(500), "stats"),
        (httpx.Response(404), "stats"),
        (httpx.Response(200, text="not json " + address), "stats"),
        (ok(fx.without(fx.stats(), "chain_stats")), "stats"),
        (ok([fx.without(fx.tx(), "fee")]), "txs"),
        (ok([fx.without(fx.tx(), "vin", 0, "prevout")]), "txs"),
    ]
    for response, method in failures:
        client = Env(response).client()
        with pytest.raises(DisplayableError) as caught:
            await (client.stats(address) if method == "stats" else client.txs(address))
        assert address not in str(caught.value)


async def test_malformed_message_names_endpoint_and_field_only() -> None:
    with pytest.raises(MalformedResponse) as caught:
        await Env(ok([fx.without(fx.tx(), "vin", 0, "prevout")])).client().txs(fx.RECEIVING_0)
    message = str(caught.value)
    assert "vin[].prevout" in message
    assert fx.TXID_A not in message


@pytest.mark.parametrize("address", ["", "bc1/../x", "a b", "x?y", "é"])
async def test_an_address_that_is_not_one_token_is_refused_unsent(address: str) -> None:
    env = Env(ok(fx.stats()))
    with pytest.raises(DisplayableError) as caught:
        await env.client().stats(address)
    assert env.sent == []
    assert address not in str(caught.value) or address == ""


def test_parse_stats_is_exported_for_the_sync() -> None:
    assert parse_stats(loads_decimal(fx.body(fx.stats()))).chain.tx_count == 0
