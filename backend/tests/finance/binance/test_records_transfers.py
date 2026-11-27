"""Wallet-to-wallet transfers, dust, Cloud Mining and the travel-rule histories in the ledger
(design 5.7 rules 1 and 2, 5.12): a transfer is one transaction on two Binance accounts, a
movement two endpoints list is posted once, dust is a swap into BNB with its fee, and a
travel-rule row describes a deposit or withdrawal without posting it again."""

from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.connections import get_connection
from lararium.modules.finance.binance import normalize
from lararium.modules.finance.binance.catalogue import (
    ENDPOINTS,
    FIELDS,
    ISOLATED_TRANSFER_TYPES,
    WALLETS,
)
from lararium.modules.finance.binance.fetch import (
    HISTORY_LABELS,
    PHASE_B_BUILT,
    cursor_keys,
    isolated_symbols,
    walk_keys,
)
from lararium.modules.finance.binance.records import Run
from lararium.modules.finance.binance.records.transfers import (
    TRANSFER_WALLETS,
    Listing,
    Movement,
    post_movement,
)

from .fixtures import BASE_TIME_MS, HOUR_MS, FakeBinance, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    keys,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    typed,
)
from .syncing import (
    CONNECTOR,
    Clock,
    RecordingNotifier,
    binance_connection,
    context,
    later_config,
    run_binance_sync,
)

TRANSFERS = "universal-transfer"
DUST = "dust"
CLOUD = "cloud-mining"
TRAVEL_DEPOSITS = "travel-deposits"
TRAVEL_WITHDRAWALS = "travel-withdrawals"
_CHANGED = "changed by something no record explains"


def _transfer(seq: int = 0, **fields) -> dict:
    values = {"asset": "AAA", "amount": "4", "type": "MAIN_FUNDING", "status": "CONFIRMED"}
    return build_row(TRANSFERS, seq, **(values | fields))


def _detail(
    from_asset: str, amount: str, received: str, fee: str, at_ms: int = BASE_TIME_MS
) -> dict:
    return {
        "transId": 0,
        "serviceChargeAmount": fee,
        "amount": amount,
        "operateTime": at_ms,
        "transferedAmount": received,
        "fromAsset": from_asset,
        "targetAsset": "BNB",
    }


def _dust(seq: int = 0, *details: dict, at_ms: int = BASE_TIME_MS) -> dict:
    trans_id = 5_000 + seq
    lines = list(details) or [_detail("AAA", "0.3", "0.01", "0.0002", at_ms)]
    received = sum((Decimal(str(d["transferedAmount"])) for d in lines), Decimal(0))
    charged = sum((Decimal(str(d["serviceChargeAmount"])) for d in lines), Decimal(0))
    return build_row(
        DUST,
        seq,
        transId=trans_id,
        operateTime=at_ms,
        totalTransferedAmount=str(received),
        totalServiceChargeAmount=str(charged),
        userAssetDribbletDetails=[{**d, "transId": trans_id} for d in lines],
    )


async def _never_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


# The type enum and the wallets.


def test_transfer_types_cover_the_catalogue_enum() -> None:
    [spec] = [f for f in FIELDS[TRANSFERS] if f.path == "rows[].type"]
    assert spec.enum is not None
    assert set(TRANSFER_WALLETS) == set(spec.enum)
    assert len(TRANSFER_WALLETS) == 31
    known = set(WALLETS.values())
    for kind, (source, destination) in TRANSFER_WALLETS.items():
        assert {source, destination} <= known, kind
    assert TRANSFER_WALLETS["MAIN_UMFUTURE"] == ("spot", "um_futures")
    assert TRANSFER_WALLETS["MARGIN_CMFUTURE"] == ("cross_margin", "cm_futures")
    assert TRANSFER_WALLETS["ISOLATEDMARGIN_MARGIN"] == ("isolated_margin", "cross_margin")
    assert TRANSFER_WALLETS["FUNDING_OPTION"] == ("funding", "options")
    assert TRANSFER_WALLETS["PORTFOLIO_MARGIN_MAIN"] == ("portfolio_margin", "spot")
    # Every type is asked: the 28 as variants, the 3 isolated ones per isolated symbol.
    [(name, walked)] = ENDPOINTS[TRANSFERS].variants
    assert name == "type"
    assert set(walked) | set(ISOLATED_TRANSFER_TYPES) == set(spec.enum)
    assert not set(walked) & set(ISOLATED_TRANSFER_TYPES)


def test_isolated_types_are_asked_once_per_isolated_symbol() -> None:
    plain_keys = cursor_keys(ENDPOINTS[TRANSFERS])
    assert walk_keys(TRANSFERS, ()) == plain_keys
    found = walk_keys(TRANSFERS, {"BBBSTB", "AAASTB"})[len(plain_keys) :]
    assert found == [
        (
            "universal-transfer:ISOLATEDMARGIN_MARGIN:AAASTB",
            {"type": "ISOLATEDMARGIN_MARGIN", "fromSymbol": "AAASTB"},
        ),
        (
            "universal-transfer:MARGIN_ISOLATEDMARGIN:AAASTB",
            {"type": "MARGIN_ISOLATEDMARGIN", "toSymbol": "AAASTB"},
        ),
        (
            "universal-transfer:ISOLATEDMARGIN_MARGIN:BBBSTB",
            {"type": "ISOLATEDMARGIN_MARGIN", "fromSymbol": "BBBSTB"},
        ),
        (
            "universal-transfer:MARGIN_ISOLATEDMARGIN:BBBSTB",
            {"type": "MARGIN_ISOLATEDMARGIN", "toSymbol": "BBBSTB"},
        ),
        (
            "universal-transfer:ISOLATEDMARGIN_ISOLATEDMARGIN:AAASTB:BBBSTB",
            {"type": "ISOLATEDMARGIN_ISOLATEDMARGIN", "fromSymbol": "AAASTB", "toSymbol": "BBBSTB"},
        ),
        (
            "universal-transfer:ISOLATEDMARGIN_ISOLATEDMARGIN:BBBSTB:AAASTB",
            {"type": "ISOLATEDMARGIN_ISOLATEDMARGIN", "fromSymbol": "BBBSTB", "toSymbol": "AAASTB"},
        ),
    ]
    # Another label has no isolated keys.
    assert walk_keys(DUST, {"AAASTB"}) == cursor_keys(ENDPOINTS[DUST])


async def test_no_isolated_symbol_is_known_yet(session, run) -> None:
    connection = await binance_connection(run)
    async with httpx.AsyncClient() as http:
        ctx = await context(session, connection.id, http)
        assert await isolated_symbols(ctx) == frozenset()


def test_the_family_is_walked() -> None:
    family = {TRANSFERS, DUST, CLOUD, TRAVEL_DEPOSITS, TRAVEL_WITHDRAWALS}
    assert set(PHASE_B_BUILT) >= family
    assert family <= set(HISTORY_LABELS)


# Wallet-to-wallet transfers (rule 1).


async def test_universal_transfer_is_one_transaction_on_two_wallets(session, run) -> None:
    connection = await binance_connection(run)
    to_funding = _transfer(0)
    from_futures = _transfer(1, type="UMFUTURE_MAIN", asset="BBB", amount="2.5")
    pending = _transfer(2, status="PENDING")
    failed = _transfer(3, status="FAILED")

    await normalize_rows(
        session, connection.id, (TRANSFERS, [to_funding, from_futures, pending, failed])
    )

    key = f"universal_transfer:{to_funding['tranId']}"
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "AAA", Decimal(-4)),
        ("Binance Funding", "AAA", Decimal(4)),
    ]
    assert plain(await postings(session, f"universal_transfer:{from_futures['tranId']}")) == [
        ("Binance USDⓈ-M Futures", "BBB", Decimal("-2.5")),
        ("Binance Spot", "BBB", Decimal("2.5")),
    ]
    assert await keys(session, connection.id) == sorted(
        [key, f"universal_transfer:{from_futures['tranId']}"]
    )
    # One record describes both sides: no In transit, and never a transfer leg.
    assert ("In transit", "AAA") not in await holdings(session, connection.id)
    assert await _never_transfer(session, key) is True
    rows = {r.tran_id: r for r in await typed(session, "binance_universal_transfer", connection.id)}
    assert rows[str(to_funding["tranId"])].transaction_id is not None
    assert rows[str(pending["tranId"])].transaction_id is None
    assert rows[str(failed["tranId"])].transaction_id is None
    await assert_balanced_and_quiet(session, connection.id)


async def test_pending_transfer_posts_once_confirmed(session, run) -> None:
    connection = await binance_connection(run)
    pending = _transfer(0, status="PENDING")
    await normalize_rows(session, connection.id, (TRANSFERS, [pending]))
    assert await keys(session, connection.id) == []

    await normalize_rows(session, connection.id, (TRANSFERS, [{**pending, "status": "CONFIRMED"}]))

    assert await keys(session, connection.id) == [f"universal_transfer:{pending['tranId']}"]
    await assert_balanced_and_quiet(session, connection.id)


async def test_unknown_transfer_type_is_flagged_not_posted(session, run) -> None:
    connection = await binance_connection(run)

    report = await normalize_rows(
        session, connection.id, (TRANSFERS, [_transfer(type="MAIN_NEWWALLET")])
    )

    assert report.new_flags == ["universal-transfer.type=MAIN_NEWWALLET"]
    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_universal_transfer", connection.id)) == 1
    after = await get_connection(session, connection.id)
    assert "Binance flagged: universal-transfer.type=MAIN_NEWWALLET" in after.warnings


async def test_isolated_transfer_keeps_the_symbols_it_was_asked_for(session, run) -> None:
    connection = await binance_connection(run)
    out = _transfer(0, type="ISOLATEDMARGIN_MARGIN")
    across = _transfer(1, type="ISOLATEDMARGIN_ISOLATEDMARGIN")
    await ingest(
        session,
        connection.id,
        TRANSFERS,
        [out],
        request={"type": "ISOLATEDMARGIN_MARGIN", "fromSymbol": "AAASTB"},
    )
    await ingest(
        session,
        connection.id,
        TRANSFERS,
        [across],
        request={
            "type": "ISOLATEDMARGIN_ISOLATEDMARGIN",
            "fromSymbol": "AAASTB",
            "toSymbol": "BBBSTB",
        },
    )

    await run_normalize(session, connection.id)

    assert plain(await postings(session, f"universal_transfer:{out['tranId']}")) == [
        ("Binance Isolated Margin", "AAA", Decimal(-4)),
        ("Binance Cross Margin", "AAA", Decimal(4)),
    ]
    rows = {r.tran_id: r for r in await typed(session, "binance_universal_transfer", connection.id)}
    assert (rows[str(out["tranId"])].from_symbol, rows[str(out["tranId"])].to_symbol) == (
        "AAASTB",
        None,
    )
    assert (rows[str(across["tranId"])].from_symbol, rows[str(across["tranId"])].to_symbol) == (
        "AAASTB",
        "BBBSTB",
    )
    await assert_balanced_and_quiet(session, connection.id)


# A movement two endpoints list (rule 2), through a second listing a later task adds.


class _Second:
    """A synthetic second listing of wallet-to-wallet movements: what a margin transfer or a
    futures income TRANSFER row will be, held in memory."""

    def __init__(self) -> None:
        self.rows: list[Movement] = []
        self.links: dict[str, str | None] = {}

    async def movements(self, run: Run) -> list[Movement]:
        return [replace(m, linked_to=self.links.get(m.key)) for m in self.rows]

    async def link(self, run: Run, movement: Movement, target: str | None, transaction_id) -> None:
        self.links[movement.key] = target

    def listing(self) -> Listing:
        return Listing("synthetic", self.movements, self.link)


async def _post_as_its_handler(session, connection_id, movement: Movement) -> None:
    """What the second listing's own handler does with a row not linked yet: rule 1."""
    async with httpx.AsyncClient() as http:
        ctx = await context(session, connection_id, http)
        async with command_context(session, CONNECTOR, "binance.normalize", {}) as cmd:
            current = await Run.start(ctx, cmd, await get_connection(session, connection_id))
            await post_movement(current, movement)


@pytest.mark.parametrize("universal_first", [True, False])
async def test_movement_listed_twice_is_posted_once(
    session, run, monkeypatch: pytest.MonkeyPatch, universal_first: bool
) -> None:
    connection = await binance_connection(run)
    second = _Second()
    monkeypatch.setattr(
        normalize, "MOVEMENT_LISTINGS", (*normalize.MOVEMENT_LISTINGS, second.listing())
    )
    transfer = _transfer(0, type="MAIN_UMFUTURE", timestamp=BASE_TIME_MS)
    universal_key = f"universal_transfer:{transfer['tranId']}"
    listed = Movement(
        key="synthetic:1",
        asset="AAA",
        amount=Decimal(4),
        wallets=("spot", "um_futures"),
        at=at(BASE_TIME_MS + 45_000),
    )
    # Another movement of the same asset and wallets, too far away in time: its own.
    apart = replace(listed, key="synthetic:2", at=at(BASE_TIME_MS + 61_000))

    if universal_first:
        await normalize_rows(session, connection.id, (TRANSFERS, [transfer]))
        second.rows = [listed, apart]
        for movement in second.rows:
            await _post_as_its_handler(session, connection.id, movement)
        await run_normalize(session, connection.id)
    else:
        second.rows = [listed, apart]
        for movement in second.rows:
            await _post_as_its_handler(session, connection.id, movement)
        await run_normalize(session, connection.id)
        assert await keys(session, connection.id) == ["synthetic:1", "synthetic:2"]
        await normalize_rows(session, connection.id, (TRANSFERS, [transfer]))

    assert await keys(session, connection.id) == ["synthetic:2", universal_key]
    assert second.links == {"synthetic:1": universal_key}
    assert (await holdings(session, connection.id)) == {
        ("Binance Spot", "AAA"): Decimal(-8),
        ("Binance USDⓈ-M Futures", "AAA"): Decimal(8),
    }
    await assert_balanced_and_quiet(session, connection.id)

    # The universal transfer no longer confirmed: the second listing posts the movement itself.
    await normalize_rows(session, connection.id, (TRANSFERS, [{**transfer, "status": "FAILED"}]))
    assert await keys(session, connection.id) == ["synthetic:1", "synthetic:2"]
    assert second.links == {"synthetic:1": None}
    await assert_balanced_and_quiet(session, connection.id)


# Dust and Cloud Mining.


async def test_dust_swaps_into_bnb_with_its_fee(session, run) -> None:
    connection = await binance_connection(run)
    dust = _dust(
        0,
        _detail("AAA", "0.306", "0.01", "0.0002"),
        _detail("BBB", "2.04", "0.04", "0.0008"),
    )
    on_margin = _dust(1, _detail("CCC", "0.5", "0.02", "0.0004"))
    before = await holdings(session, connection.id)
    await ingest(session, connection.id, DUST, [dust], request={"accountType": "SPOT"})
    await ingest(session, connection.id, DUST, [on_margin], request={"accountType": "MARGIN"})

    await run_normalize(session, connection.id)

    # One transaction per conversion, each line a swap into the BNB it bought (credited plus
    # charge) weighed by its cost, and the service charges as fees against one Unassigned
    # posting: each line's BNB changes by its transferedAmount, the BNB credited.
    key = f"dust:{dust['transId']}"
    fee = "service charge"
    assert await postings(session, key) == [
        ("Binance Spot", "AAA", Decimal("-0.306"), None, None, None, None, False, ""),
        ("Binance Spot", "BNB", Decimal("0.0102"), Decimal(30), "AAA", None, None, False, ""),
        ("Binance Spot", "BBB", Decimal("-2.04"), None, None, None, None, False, ""),
        ("Binance Spot", "BNB", Decimal("0.0408"), Decimal(50), "BBB", None, None, False, ""),
        ("Binance Spot", "BNB", Decimal("-0.0002"), None, None, None, None, True, fee),
        ("Binance Spot", "BNB", Decimal("-0.0008"), None, None, None, None, True, fee),
        ("Unassigned", "BNB", Decimal("0.0010"), None, None, None, None, False, ""),
    ]
    assert plain(await postings(session, f"dust:{on_margin['transId']}")) == [
        ("Binance Cross Margin", "CCC", Decimal("-0.5")),
        ("Binance Cross Margin", "BNB", Decimal("0.0204")),
        ("Binance Cross Margin", "BNB", Decimal("-0.0004")),
        ("Unassigned", "BNB", Decimal("0.0004")),
    ]
    assert await _never_transfer(session, key) is True
    after = await holdings(session, connection.id)
    assert before == {}
    assert after[("Binance Spot", "BNB")] == Decimal("0.05")  # 0.01 + 0.04 credited
    assert after[("Binance Cross Margin", "BNB")] == Decimal("0.02")
    rows = {r.trans_id: r for r in await typed(session, "binance_dust", connection.id)}
    assert rows[str(dust["transId"])].account_type == "SPOT"
    assert rows[str(on_margin["transId"])].account_type == "MARGIN"
    assert all(row.transaction_id is not None for row in rows.values())
    lines = await session.scalar(
        text(
            "select count(*) from finance.binance_dust_user_asset_dribblet_details"
            " where connection_id = :c"
        ),
        {"c": connection.id},
    )
    assert lines == 3
    await assert_balanced_and_quiet(session, connection.id)


async def test_dust_without_its_account_type_is_flagged_not_posted(session, run) -> None:
    connection = await binance_connection(run)
    await ingest(session, connection.id, DUST, [_dust(0)])

    report = await run_normalize(session, connection.id)

    assert report.new_flags == ["dust.accountType missing"]
    assert await keys(session, connection.id) == []


async def test_cloud_mining_payment_and_refund(session, run) -> None:
    connection = await binance_connection(run)
    payment = build_row(CLOUD, 0, type=248, asset="AAA", amount="3", status="S")
    refund = build_row(CLOUD, 1, type=249, asset="AAA", amount="1", status="S")

    await normalize_rows(session, connection.id, (CLOUD, [payment, refund]))

    assert plain(await postings(session, f"cloud_mining:{payment['tranId']}")) == [
        ("Binance Spot", "AAA", Decimal(-3)),
        ("Unassigned", "AAA", Decimal(3)),
    ]
    assert plain(await postings(session, f"cloud_mining:{refund['tranId']}")) == [
        ("Binance Spot", "AAA", Decimal(1)),
        ("Unassigned", "AAA", Decimal(-1)),
    ]
    assert await _never_transfer(session, f"cloud_mining:{payment['tranId']}") is True
    await assert_balanced_and_quiet(session, connection.id)


async def test_unknown_cloud_mining_type_is_flagged_not_posted(session, run) -> None:
    connection = await binance_connection(run)

    report = await normalize_rows(session, connection.id, (CLOUD, [build_row(CLOUD, type=250)]))

    assert report.new_flags == ["cloud-mining.type=250"]
    assert await keys(session, connection.id) == []


# The travel-rule histories: the same movements, more fields, never posted.


@pytest.mark.parametrize("travel_first", [True, False])
async def test_travel_rule_row_never_posts_twice(session, run, travel_first: bool) -> None:
    connection = await binance_connection(run)
    deposit = build_row(
        "deposits", 0, id="7001", coin="AAA", amount="5", status=1, walletType=0, transferType=1
    )
    withdrawal = build_row(
        "withdrawals",
        0,
        id="w-7002",
        coin="AAA",
        amount="2",
        transactionFee="0.1",
        status=6,
        walletType=0,
        transferType=1,
    )
    travel_deposit = build_row(TRAVEL_DEPOSITS, 0, tranId=7001, coin="AAA", amount="5")
    travel_withdrawal = build_row(TRAVEL_WITHDRAWALS, 0, id="w-7002", coin="AAA", amount="2")
    capital = (("deposits", [deposit]), ("withdrawals", [withdrawal]))
    travel = ((TRAVEL_DEPOSITS, [travel_deposit]), (TRAVEL_WITHDRAWALS, [travel_withdrawal]))

    for batches in (travel, capital) if travel_first else (capital, travel):
        await normalize_rows(session, connection.id, *batches)

    assert await keys(session, connection.id) == sorted(
        ["deposit:7001", "withdrawal:w-7002", "withdrawal:w-7002:fee"]
    )
    assert (await holdings(session, connection.id))[("Binance Spot", "AAA")] == Decimal("2.9")
    [linked_deposit] = await typed(session, "binance_travel_deposits", connection.id)
    [linked_withdrawal] = await typed(session, "binance_travel_withdrawals", connection.id)
    assert (linked_deposit.deposit_id, linked_deposit.transaction_id) == ("7001", None)
    assert (linked_withdrawal.withdrawal_id, linked_withdrawal.transaction_id) == ("w-7002", None)
    await assert_balanced_and_quiet(session, connection.id)


async def test_travel_rule_row_without_its_movement_is_unlinked(session, run) -> None:
    connection = await binance_connection(run)

    await normalize_rows(
        session, connection.id, (TRAVEL_DEPOSITS, [build_row(TRAVEL_DEPOSITS, 0, tranId=9)])
    )

    [row] = await typed(session, "binance_travel_deposits", connection.id)
    assert row.deposit_id is None
    assert await keys(session, connection.id) == []


# Reconciliation: Spot and Funding with a consistent history of the family's records.


def _spot(*balances: tuple[str, str]) -> list[dict]:
    return [build_row("spot-account", asset=code, free=free, locked="0") for code, free in balances]


def _funding(*balances: tuple[str, str]) -> list[dict]:
    return [
        build_row("funding-asset", asset=code, free=free, locked="0", freeze="0", withdrawing="0")
        for code, free in balances
    ]


async def _differences(session, connection_id) -> list:
    rows = await session.execute(
        text(
            "select a.name, i.symbol from finance.binance_reconciliation_differences d"
            " join finance.accounts a on a.id = d.account_id"
            " join finance.instruments i on i.id = d.instrument_id"
            " where a.source_connection_id = :c"
        ),
        {"c": connection_id},
    )
    return list(rows)


async def test_spot_and_funding_reconcile_with_transfers_dust_and_mining(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("spot-account", _spot(("AAA", "10"), ("BBB", "1")))
    fake.set_rows("funding-asset", _funding(("AAA", "2")))
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error
    start = clock.ms
    fake.add(
        TRANSFERS,
        _transfer(0, type="MAIN_FUNDING", amount="4", timestamp=start + HOUR_MS),
        when={"type": "MAIN_FUNDING"},
    )
    fake.add(
        TRANSFERS,
        _transfer(1, type="FUNDING_MAIN", amount="1", timestamp=start + 2 * HOUR_MS),
        when={"type": "FUNDING_MAIN"},
    )
    dust_at = start + 3 * HOUR_MS
    dust = _dust(0, _detail("BBB", "1", "0.01", "0.0002", dust_at), at_ms=dust_at)
    fake.add(DUST, dust, when={"accountType": "SPOT"})
    fake.add(CLOUD, build_row(CLOUD, 0, type=248, amount="0.5", createTime=start + 4 * HOUR_MS))
    fake.set_rows("spot-account", _spot(("AAA", "6.5"), ("BNB", "0.01")))
    fake.set_rows("funding-asset", _funding(("AAA", "5")))
    notifier = RecordingNotifier()

    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert len(await keys(session, connection.id)) >= 4
    assert await _differences(session, connection.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    # The isolated types are not asked until an isolated symbol is known.
    asked = {s.params["type"] for s in fake.calls(TRANSFERS)}
    assert asked == {k for k in TRANSFER_WALLETS if k not in ISOLATED_TRANSFER_TYPES}
