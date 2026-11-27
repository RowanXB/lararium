"""The other Earn products in the ledger (design 5.7): locked Simple Earn and on-chain yields
move between a wallet and Earn as flexible does; BFUSD, RWUSD, ETH and SOL staking and WBETH
wraps are swaps on Spot, a redemption that arrives later passing through the Pending
redemptions account; rate rewards are typed only; every other reward is income on its
product's wallet unless an asset dividend paid it (rule 3); dual investment and discount buy
post from their position rows."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from lararium.modules.finance.binance import normalize as normalize_module
from lararium.modules.finance.binance.balances import Read, store_reports
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import (
    BALANCE_LABELS,
    HISTORY_LABELS,
    PHASE_B_BUILT,
    to_json,
)
from lararium.modules.finance.binance.records import Run, unit_cost
from lararium.modules.finance.transfers import find_transfer_violations

from .fixtures import BASE_TIME_MS, DAY_MS, HOUR_MS, FakeBinance, build_response, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    ingest_report,
    keys,
    ledger,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    transaction,
    typed,
)
from .syncing import (
    Clock,
    RecordingNotifier,
    binance_connection,
    later_config,
    run_binance_sync,
)

T = BASE_TIME_MS
PENDING = "Binance Pending redemptions"
_CHANGED = "changed by something no record explains"

HISTORIES = (
    "earn-locked-subscriptions",
    "earn-locked-redemptions",
    "earn-locked-rewards",
    "earn-flexible-collateral",
    "bfusd-subscriptions",
    "bfusd-redemptions",
    "bfusd-rewards",
    "rwusd-subscriptions",
    "rwusd-redemptions",
    "rwusd-rewards",
    "eth-stakes",
    "eth-redemptions",
    "wbeth-rewards",
    "wbeth-wraps",
    "wbeth-unwraps",
    "sol-stakes",
    "sol-redemptions",
    "bnsol-rewards",
    "sol-boost-rewards",
    "onchain-subscriptions",
    "onchain-redemptions",
    "onchain-rewards",
    "soft-staking-rewards",
)
REPORTS = (
    "earn-locked-position",
    "bfusd-account",
    "rwusd-account",
    "eth-staking-account",
    "sol-staking-account",
    "sol-unclaimed-rewards",
    "onchain-account",
    "onchain-locked-position",
    "soft-staking-list",
    "dci-accounts",
    "dci-positions",
    "discount-accounts",
    "discount-positions",
)


def test_the_family_is_walked_and_read() -> None:
    assert set(PHASE_B_BUILT) >= {*HISTORIES, *REPORTS}
    assert set(HISTORIES) <= set(HISTORY_LABELS)
    assert set(REPORTS) <= set(BALANCE_LABELS)
    # The BFUSD and RWUSD histories refuse a documented 6-month window (catalogue, Doc vs real).
    for label in HISTORIES:
        if label.startswith(("bfusd-", "rwusd-")):
            assert ENDPOINTS[label].window_days == 29, label


# Locked Simple Earn and on-chain yields: between a wallet and Earn (rule 1).


def _locked_subscription(seq: int = 0, **fields: Any) -> dict:
    values = {"asset": "AAA", "amount": "4", "status": "SUCCESS", "sourceAccount": "SPOT"}
    return build_row("earn-locked-subscriptions", seq, **(values | fields))


async def test_locked_subscription_moves_between_wallets(session, run) -> None:
    connection = await binance_connection(run)
    from_spot = _locked_subscription(0)
    from_funding = _locked_subscription(1, sourceAccount="FUNDING")
    from_both = _locked_subscription(
        2, sourceAccount="SPOTANDFUNDING", amtFromSpot="1", amtFromFunding="3"
    )
    purchasing = _locked_subscription(3, status="PURCHASING")

    await normalize_rows(
        session,
        connection.id,
        ("earn-locked-subscriptions", [from_spot, from_funding, from_both, purchasing]),
    )

    def key(row: dict) -> str:
        return f"earn_locked_subscription:{row['purchaseId']}"

    assert plain(await postings(session, key(from_spot))) == [
        ("Binance Spot", "AAA", Decimal(-4)),
        ("Binance Earn", "AAA", Decimal(4)),
    ]
    assert plain(await postings(session, key(from_funding))) == [
        ("Binance Funding", "AAA", Decimal(-4)),
        ("Binance Earn", "AAA", Decimal(4)),
    ]
    assert plain(await postings(session, key(from_both))) == [
        ("Binance Spot", "AAA", Decimal(-1)),
        ("Binance Funding", "AAA", Decimal(-3)),
        ("Binance Earn", "AAA", Decimal(4)),
    ]
    assert key(purchasing) not in await keys(session, connection.id)
    assert ("In transit", "AAA") not in await holdings(session, connection.id)
    await assert_balanced_and_quiet(session, connection.id)


def _locked_redemption(seq: int, kind: str, **fields: Any) -> dict:
    values = {"asset": "AAA", "amount": "3", "type": kind, "status": "PAID"}
    return build_row("earn-locked-redemptions", seq, **(values | fields))


async def test_locked_redemption_returns_to_spot(session, run) -> None:
    connection = await binance_connection(run)
    mature, early = _locked_redemption(0, "MATURE"), _locked_redemption(1, "AHEAD")
    # To Simple Earn flexible: the asset stays in the Earn wallet.
    to_flexible = _locked_redemption(2, "NEW_TRANSFERRED")
    unpaid = _locked_redemption(3, "MATURE", status="PROCESSING")

    await normalize_rows(
        session, connection.id, ("earn-locked-redemptions", [mature, early, to_flexible, unpaid])
    )

    for row in (mature, early):
        assert plain(await postings(session, f"earn_locked_redemption:{row['redeemId']}")) == [
            ("Binance Earn", "AAA", Decimal(-3)),
            ("Binance Spot", "AAA", Decimal(3)),
        ]
    assert await keys(session, connection.id) == sorted(
        f"earn_locked_redemption:{row['redeemId']}" for row in (mature, early)
    )
    assert len(await typed(session, "binance_earn_locked_redemptions", connection.id)) == 4
    await assert_balanced_and_quiet(session, connection.id)


async def test_onchain_subscription_and_redemption(session, run) -> None:
    connection = await binance_connection(run)
    subscription = build_row(
        "onchain-subscriptions", 0, asset="AAA", amount="5", status="SUCCESS", sourceAccount="SPOT"
    )
    failed = build_row(
        "onchain-subscriptions", 1, asset="AAA", amount="5", status="FAILED", sourceAccount="SPOT"
    )

    def redemption(seq: int, kind: str, status: str = "PAID") -> dict:
        return build_row(
            "onchain-redemptions", seq, asset="AAA", amount="2", type=kind, status=status
        )

    normal, early = redemption(0, "NORMAL"), redemption(1, "EARLY")
    converted, created = redemption(2, "CONVERT"), redemption(3, "NORMAL", "CREATED")

    await normalize_rows(
        session,
        connection.id,
        ("onchain-subscriptions", [subscription, failed]),
        ("onchain-redemptions", [normal, early, converted, created]),
    )

    assert plain(await postings(session, f"onchain_subscription:{subscription['purchaseId']}")) == [
        ("Binance Spot", "AAA", Decimal(-5)),
        ("Binance Earn", "AAA", Decimal(5)),
    ]
    for row in (normal, early):
        assert plain(await postings(session, f"onchain_redemption:{row['redeemId']}")) == [
            ("Binance Earn", "AAA", Decimal(-2)),
            ("Binance Spot", "AAA", Decimal(2)),
        ]
    assert len(await keys(session, connection.id)) == 3
    assert await holdings(session, connection.id) == {
        ("Binance Spot", "AAA"): Decimal(-1),
        ("Binance Earn", "AAA"): Decimal(1),
    }
    await assert_balanced_and_quiet(session, connection.id)


# Rewards (rule 3): income on the product's wallet, unless an asset dividend paid it.


def _reward_row(label: str, coin: str, quantity: str, at_ms: int) -> dict:
    match label:
        case "earn-flexible-rewards":
            return build_row(label, asset=coin, rewards=quantity, type="BONUS", time=at_ms)
        case "earn-locked-rewards":
            return build_row(label, asset=coin, amount=quantity, type="Locked Rewards", time=at_ms)
        case "onchain-rewards":
            return build_row(label, asset=coin, amount=quantity, time=at_ms)
        case "bfusd-rewards":
            return build_row(label, rewardAsset=coin, rewardsAmount=quantity, time=at_ms)
        case "rwusd-rewards":
            return build_row(label, rewardsAmount=quantity, time=at_ms)
        case "soft-staking-rewards":
            return build_row(label, asset="BBB", rewardAsset=coin, rewards=quantity, time=at_ms)
        case "sol-boost-rewards":
            return build_row(label, token=coin, amount=quantity, status="SUCCESS", time=at_ms)
        case _:
            raise AssertionError(label)


# Each reward label: the asset its reward is in (RWUSD's row names none) and the wallet an
# unlinked reward accrues on.
REWARDS = {
    "earn-flexible-rewards": ("AAA", "Binance Earn"),
    "earn-locked-rewards": ("AAA", "Binance Earn"),
    "onchain-rewards": ("AAA", "Binance Earn"),
    "bfusd-rewards": ("AAA", "Binance Spot"),
    "rwusd-rewards": ("RWUSD", "Binance Spot"),
    "soft-staking-rewards": ("AAA", "Binance Spot"),
    "sol-boost-rewards": ("AAA", "Binance Spot"),
}
# The request a reward answered: boost rewards are asked per type.
_ASKED = {"sol-boost-rewards": {"type": "CLAIM"}}


def _table(label: str) -> str:
    return "binance_" + label.replace("-", "_")


def _prefix(label: str) -> str:
    return label.replace("-", "_").removesuffix("s") + ":"


async def _ingest_reward(session, connection_id, label: str, row: dict) -> None:
    await ingest(session, connection_id, label, [row], request=_ASKED.get(label))


@pytest.mark.parametrize("label", list(REWARDS))
async def test_reward_with_a_dividend_is_linked_for_every_product(session, run, label: str) -> None:
    connection = await binance_connection(run)
    coin, _ = REWARDS[label]
    reward = _reward_row(label, coin, "0.7", T)
    dividend = build_row(
        "dividends", asset=coin, amount="0.7", direction=1, divTime=T + 3 * HOUR_MS
    )

    await _ingest_reward(session, connection.id, label, reward)
    await run_normalize(session, connection.id)
    # Posted on its product until the dividend arrives.
    assert [k for k in await keys(session, connection.id) if k.startswith(_prefix(label))]
    await normalize_rows(session, connection.id, ("dividends", [dividend]))

    assert await keys(session, connection.id) == [f"dividend:{dividend['id']}"]
    assert await holdings(session, connection.id) == {
        ("Binance Spot", coin): Decimal("0.7"),
        ("Unassigned", coin): Decimal("-0.7"),
    }
    [row] = await typed(session, _table(label), connection.id)
    assert (row.dividend_id, row.transaction_id) == (str(dividend["id"]), None)
    await assert_balanced_and_quiet(session, connection.id)
    # Regenerating from the raw records links it again.
    await run_normalize(session, connection.id, again=True)
    assert await keys(session, connection.id) == [f"dividend:{dividend['id']}"]


@pytest.mark.parametrize("label", list(REWARDS))
async def test_reward_without_a_dividend_accrues_on_its_product(session, run, label: str) -> None:
    connection = await binance_connection(run)
    coin, wallet = REWARDS[label]
    reward = _reward_row(label, coin, "0.25", T)
    # Another amount, or too late: not its payout.
    other = build_row("dividends", 0, asset=coin, amount="0.3", direction=1, divTime=T)
    late = build_row(
        "dividends", 1, asset=coin, amount="0.25", direction=1, divTime=T + 27 * HOUR_MS
    )

    await _ingest_reward(session, connection.id, label, reward)
    await normalize_rows(session, connection.id, ("dividends", [other, late]))

    [key] = [k for k in await keys(session, connection.id) if k.startswith(_prefix(label))]
    assert plain(await postings(session, key)) == [
        (wallet, coin, Decimal("0.25")),
        ("Unassigned", coin, Decimal("-0.25")),
    ]
    [row] = await typed(session, _table(label), connection.id)
    assert row.dividend_id is None and row.transaction_id is not None
    await assert_balanced_and_quiet(session, connection.id)


@pytest.mark.parametrize("label", ["earn-locked-rewards", "soft-staking-rewards"])
async def test_dividend_before_its_reward_gives_the_same_ledger(session, run, label: str) -> None:
    coin, _ = REWARDS[label]
    reward = _reward_row(label, coin, "0.7", T)
    dividend = build_row("dividends", asset=coin, amount="0.7", direction=1, divTime=T - HOUR_MS)
    reward_first = await binance_connection(run)
    await _ingest_reward(session, reward_first.id, label, reward)
    await run_normalize(session, reward_first.id)
    await normalize_rows(session, reward_first.id, ("dividends", [dividend]))
    dividend_first = await binance_connection(run)
    await normalize_rows(session, dividend_first.id, ("dividends", [dividend]))
    await _ingest_reward(session, dividend_first.id, label, reward)
    await run_normalize(session, dividend_first.id)

    assert await keys(session, dividend_first.id) == [f"dividend:{dividend['id']}"]
    assert await ledger(session, dividend_first.id) == await ledger(session, reward_first.id)
    [row] = await typed(session, _table(label), dividend_first.id)
    assert (row.dividend_id, row.transaction_id) == (str(dividend["id"]), None)
    await assert_balanced_and_quiet(session, dividend_first.id)


async def test_boost_distribution_is_typed_only(session, run) -> None:
    # A DISTRIBUTE row is a reward handed out to be claimed; only its claim (CLAIM) posts.
    connection = await binance_connection(run)
    distributed = build_row("sol-boost-rewards", token="AAA", amount="0.7", time=T)
    dividend = build_row("dividends", asset="AAA", amount="0.7", direction=1, divTime=T)

    await ingest(
        session, connection.id, "sol-boost-rewards", [distributed], request={"type": "DISTRIBUTE"}
    )
    await normalize_rows(session, connection.id, ("dividends", [dividend]))

    assert await keys(session, connection.id) == [f"dividend:{dividend['id']}"]
    [row] = await typed(session, "binance_sol_boost_rewards", connection.id)
    assert (row.type, row.dividend_id, row.transaction_id) == ("DISTRIBUTE", None, None)
    await assert_balanced_and_quiet(session, connection.id)


async def test_rate_rewards_post_nothing(session, run) -> None:
    # WBETH and BNSOL rewards accrue inside the token's rate: no quantity changes.
    connection = await binance_connection(run)

    await normalize_rows(
        session,
        connection.id,
        ("wbeth-rewards", [build_row("wbeth-rewards", 0), build_row("wbeth-rewards", 1)]),
        ("bnsol-rewards", [build_row("bnsol-rewards", 0)]),
    )

    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_wbeth_rewards", connection.id)) == 2
    assert len(await typed(session, "binance_bnsol_rewards", connection.id)) == 1
    await assert_balanced_and_quiet(session, connection.id)


async def test_flexible_collateral_is_typed_only(session, run) -> None:
    connection = await binance_connection(run)

    await normalize_rows(
        session,
        connection.id,
        ("earn-flexible-collateral", [build_row("earn-flexible-collateral")]),
    )

    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_earn_flexible_collateral", connection.id)) == 1
    await assert_balanced_and_quiet(session, connection.id)


# Yield tokens and staking: swaps on Spot.


def _swap_row(label: str, given: tuple[str, str], got: tuple[str, str], **fields: Any) -> dict:
    (give, gave), (get, received) = given, got
    if label in ("wbeth-wraps", "wbeth-unwraps"):
        names = {"fromAsset": give, "fromAmount": gave, "toAsset": get, "toAmount": received}
    elif label.startswith(("bfusd-", "rwusd-")):
        names = {"asset": give, "amount": gave, "receiveAsset": get, "receiveAmount": received}
    else:
        names = {
            "asset": give,
            "amount": gave,
            "distributeAsset": get,
            "distributeAmount": received,
        }
    if "arrivalTime" in build_row(label):
        fields = {"arrivalTime": T, **fields}
    if label.endswith("-redemptions") and label.startswith(("bfusd-", "rwusd-")):
        fields = {"fee": "0", **fields}
    return build_row(label, **({"time": T, "status": "SUCCESS"} | names | fields))


SWAPS = {
    "bfusd-subscriptions": (("STB", "4"), ("BFUSD", "3.9")),
    "rwusd-subscriptions": (("STB", "4"), ("RWUSD", "4")),
    "bfusd-redemptions": (("BFUSD", "3"), ("STB", "3.1")),
    "rwusd-redemptions": (("RWUSD", "3"), ("STB", "3")),
    "eth-stakes": (("ETH", "2"), ("WBETH", "1.9")),
    "eth-redemptions": (("WBETH", "1.9"), ("ETH", "2")),
    "sol-stakes": (("SOL", "3"), ("BNSOL", "2.8")),
    "sol-redemptions": (("BNSOL", "2.8"), ("SOL", "3")),
    "wbeth-wraps": (("BETH", "1"), ("WBETH", "0.98")),
    "wbeth-unwraps": (("WBETH", "0.98"), ("BETH", "1")),
}


def _swap_key(label: str, coin: str, quantity: str, at_ms: int = T, occurrence: int = 1) -> str:
    """A swap row has no id, and its status changes: it is keyed by its time, asset and amount
    (what the catalogue identifies it by) and its occurrence."""
    return f"{_prefix(label)}{at_ms}:{coin}:{quantity}:{occurrence}"


@pytest.mark.parametrize("label", list(SWAPS))
async def test_staking_and_yield_tokens_are_swaps_on_spot(session, run, label: str) -> None:
    connection = await binance_connection(run)
    given, got = SWAPS[label]
    row = _swap_row(label, given, got)

    await normalize_rows(session, connection.id, (label, [row]))

    (give, gave), (get, received) = given, got
    key = _swap_key(label, give, gave)
    assert await keys(session, connection.id) == [key]
    found = await postings(session, key)
    assert [r[:5] for r in found] == [
        ("Binance Spot", give, -Decimal(gave), None, None),
        (
            "Binance Spot",
            get,
            Decimal(received),
            unit_cost(Decimal(gave), Decimal(received)),
            give,
        ),
    ]
    [typed_row] = await typed(session, _table(label), connection.id)
    assert typed_row.transaction_id is not None
    assert not await _never_transfer_false(session, connection.id)
    await assert_balanced_and_quiet(session, connection.id)


async def _never_transfer_false(session, connection_id) -> list[str]:
    """The connection's live transactions that could be a transfer leg."""
    found = await session.scalars(
        text(
            "select external_id from finance.transactions where connection_id = :c"
            " and status <> 'void' and not never_transfer"
        ),
        {"c": connection_id},
    )
    return list(found)


@pytest.mark.parametrize(
    ("label", "status"),
    [
        ("eth-stakes", "FAILED"),
        ("eth-stakes", "PENDING"),
        ("wbeth-wraps", "FAILED"),
        ("eth-redemptions", "FAILED"),
        ("bfusd-subscriptions", "OTHER"),
    ],
)
async def test_unfinished_swap_is_not_posted(session, run, label: str, status: str) -> None:
    connection = await binance_connection(run)
    given, got = SWAPS[label]

    await normalize_rows(
        session, connection.id, (label, [_swap_row(label, given, got, status=status)])
    )

    assert await keys(session, connection.id) == []
    assert len(await typed(session, _table(label), connection.id)) == 1
    await assert_balanced_and_quiet(session, connection.id)


async def test_redemption_fee_is_taken_from_what_it_received(session, run) -> None:
    # Binance's example: 51 BFUSD redeemed, 50 USDT received, a fee of 1.
    connection = await binance_connection(run)
    row = _swap_row("bfusd-redemptions", ("BFUSD", "51"), ("STB", "50"), fee="1", arrivalTime=T)

    await normalize_rows(session, connection.id, ("bfusd-redemptions", [row]))

    key = _swap_key("bfusd-redemptions", "BFUSD", "51")
    assert [r[:5] + r[7:8] for r in await postings(session, key)] == [
        ("Binance Spot", "BFUSD", Decimal(-51), None, None, False),
        ("Binance Spot", "STB", Decimal(51), Decimal(1), "BFUSD", False),
        ("Binance Spot", "STB", Decimal(-1), None, None, True),
        ("Unassigned", "STB", Decimal(1), None, None, False),
    ]
    assert (await holdings(session, connection.id))[("Binance Spot", "STB")] == Decimal(50)
    await assert_balanced_and_quiet(session, connection.id)


@pytest.mark.parametrize("label", ["bfusd-redemptions", "eth-redemptions", "sol-redemptions"])
async def test_delayed_redemption_arrives_later_through_pending_redemptions(
    session, run, label: str
) -> None:
    connection = await binance_connection(run)
    given, got = SWAPS[label]
    (give, gave), (get, received) = given, got
    arrives = T + 2 * DAY_MS
    pending = _swap_row(label, given, got, status="PENDING", arrivalTime=arrives)
    arrived = {**pending, "status": "SUCCESS"}
    key = _swap_key(label, give, gave)

    # Seen pending first: the token has left Spot, and what it buys waits for its arrival.
    await normalize_rows(session, connection.id, (label, [pending]))
    assert await keys(session, connection.id) == [key]
    assert plain(await postings(session, key)) == [
        ("Binance Spot", give, Decimal(f"-{gave}")),
        (PENDING, get, Decimal(received)),
    ]
    assert (await holdings(session, connection.id)).get(("Binance Spot", get)) is None

    # Then arrived: the received side moves to Spot at its arrival time.
    await normalize_rows(session, connection.id, (label, [arrived]))
    assert await keys(session, connection.id) == [key, f"{key}:arrival"]
    assert plain(await postings(session, f"{key}:arrival")) == [
        (PENDING, get, Decimal(f"-{received}")),
        ("Binance Spot", get, Decimal(received)),
    ]
    found = {k: when for k, _, _, _, _, when in await ledger(session, connection.id)}
    assert (found[key], found[f"{key}:arrival"]) == (at(T), at(arrives))
    assert await holdings(session, connection.id) == {
        ("Binance Spot", give): Decimal(f"-{gave}"),
        ("Binance Spot", get): Decimal(received),
    }
    # One movement Binance updated: one typed row, the version seen last.
    [kept] = await typed(session, _table(label), connection.id)
    assert (kept.status, kept.transaction_id) == ("SUCCESS", (await transaction(session, key)).id)
    # Never a transfer, never In transit: the transfers invariant holds.
    assert await _never_transfer_false(session, connection.id) == []
    assert await find_transfer_violations(session) == []
    await assert_balanced_and_quiet(session, connection.id)
    # Regenerating from the raw records (both versions) gives the same ledger.
    before = await ledger(session, connection.id)
    await run_normalize(session, connection.id, again=True)
    assert await ledger(session, connection.id) == before


async def test_redemption_that_arrived_at_once_is_one_swap(session, run) -> None:
    connection = await binance_connection(run)
    given, got = SWAPS["eth-redemptions"]

    await normalize_rows(
        session, connection.id, ("eth-redemptions", [_swap_row("eth-redemptions", given, got)])
    )

    assert await keys(session, connection.id) == [_swap_key("eth-redemptions", "WBETH", "1.9")]
    assert PENDING not in {name for name, _ in await holdings(session, connection.id)}
    await assert_balanced_and_quiet(session, connection.id)


# Dual investment and discount buy: from the position rows of each report.


def _dci(seq: int = 0, **fields: Any) -> dict:
    values = {
        "investCoin": "STB",
        "exercisedCoin": "AAA",
        "subscriptionAmount": "100",
        "purchaseStatus": "PURCHASE_SUCCESS",
        "subscriptionTime": T,
        "settleDate": T + 3 * DAY_MS,
        "duration": 3,
    }
    return build_row("dci-positions", seq, **(values | fields))


async def _occurred_at(session, key: str):
    return await session.scalar(
        text("select occurred_at from finance.transactions where external_id = :e"), {"e": key}
    )


async def test_dual_investment_settlement_leaves_earn(session, run) -> None:
    # The settled asset and amount are not documented (design 6): the invested amount leaves
    # Earn against Unassigned, and what Spot receives shows as a reconciliation difference.
    connection = await binance_connection(run)
    position, refunded, failed = (
        _dci(0),
        _dci(1, purchaseStatus="REFUND_SUCCESS"),
        _dci(2, purchaseStatus="PURCHASE_FAIL"),
    )
    subscription = f"dci_position:{position['id']}"

    # The first report: subscribed.
    await ingest_report(
        session, connection.id, "dci-positions", [position, failed], at_ms=T + HOUR_MS
    )
    await run_normalize(session, connection.id)
    assert await keys(session, connection.id) == [subscription]
    assert plain(await postings(session, subscription)) == [
        ("Binance Spot", "STB", Decimal(-100)),
        ("Binance Earn", "STB", Decimal(100)),
    ]
    assert await _occurred_at(session, subscription) == at(T)

    # Read again in a later report: nothing is written.
    await ingest_report(session, connection.id, "dci-positions", [position], at_ms=T + 2 * HOUR_MS)
    report = await run_normalize(session, connection.id)
    assert (report.created, report.updated, report.removed) == (0, 0, 0)

    # Settled, and refunded: each settles once, at its settlement date.
    settled = {**position, "purchaseStatus": "SETTLED"}
    await ingest_report(
        session, connection.id, "dci-positions", [settled, refunded], at_ms=T + 4 * DAY_MS
    )
    await run_normalize(session, connection.id)
    await ingest_report(
        session, connection.id, "dci-positions", [settled, refunded], at_ms=T + 5 * DAY_MS
    )
    report = await run_normalize(session, connection.id)
    assert (report.created, report.updated, report.removed) == (0, 0, 0)

    assert plain(await postings(session, f"{subscription}:settlement")) == [
        ("Binance Earn", "STB", Decimal(-100)),
        ("Unassigned", "STB", Decimal(100)),
    ]
    assert await _occurred_at(session, f"{subscription}:settlement") == at(T + 3 * DAY_MS)
    back = f"dci_position:{refunded['id']}"
    assert plain(await postings(session, f"{back}:settlement")) == [
        ("Binance Earn", "STB", Decimal(-100)),
        ("Binance Spot", "STB", Decimal(100)),
    ]
    assert await keys(session, connection.id) == sorted(
        [subscription, f"{subscription}:settlement", back, f"{back}:settlement"]
    )
    holding = await holdings(session, connection.id)
    assert ("Binance Earn", "STB") not in holding
    assert holding == {
        ("Binance Spot", "STB"): Decimal(-100),
        ("Unassigned", "STB"): Decimal(100),
    }
    assert await _never_transfer_false(session, connection.id) == []
    await assert_balanced_and_quiet(session, connection.id)
    before = await ledger(session, connection.id)
    await run_normalize(session, connection.id, again=True)
    assert await ledger(session, connection.id) == before


async def test_settlement_seen_before_its_date_is_dated_when_seen(session, run) -> None:
    connection = await binance_connection(run)
    early = _dci(0, purchaseStatus="SETTLED", settleDate=T + 9 * DAY_MS)

    await ingest_report(session, connection.id, "dci-positions", [early], at_ms=T + DAY_MS)
    await run_normalize(session, connection.id)
    # A later report does not move it.
    await ingest_report(session, connection.id, "dci-positions", [early], at_ms=T + 2 * DAY_MS)
    await run_normalize(session, connection.id)

    key = f"dci_position:{early['id']}:settlement"
    assert await _occurred_at(session, key) == at(T + DAY_MS)


def _discount(seq: int = 0, **fields: Any) -> dict:
    values = {
        "investAsset": "STB",
        "targetAsset": "AAA",
        "depositAmount": "50",
        "purchaseStatus": "PURCHASE_SUCCESS",
        "settleDate": T + 7 * DAY_MS,
        "duration": 7,
    }
    return build_row("discount-positions", seq, **(values | fields))


async def test_discount_buy_subscribes_its_duration_before_settling(session, run) -> None:
    connection = await binance_connection(run)
    position = _discount(0)
    key = f"discount_position:{position['id']}"

    await ingest_report(session, connection.id, "discount-positions", [position], at_ms=T + DAY_MS)
    await run_normalize(session, connection.id)
    assert plain(await postings(session, key)) == [
        ("Binance Spot", "STB", Decimal(-50)),
        ("Binance Earn", "STB", Decimal(50)),
    ]
    # No subscription time is documented: the settlement date less the duration in days.
    assert await _occurred_at(session, key) == at(T)

    settled = {**position, "purchaseStatus": "SETTLED"}
    await ingest_report(
        session, connection.id, "discount-positions", [settled], at_ms=T + 8 * DAY_MS
    )
    await run_normalize(session, connection.id)
    assert plain(await postings(session, f"{key}:settlement")) == [
        ("Binance Earn", "STB", Decimal(-50)),
        ("Unassigned", "STB", Decimal(50)),
    ]
    assert await _occurred_at(session, f"{key}:settlement") == at(T + 7 * DAY_MS)
    await assert_balanced_and_quiet(session, connection.id)


async def test_discount_buy_without_its_dates_is_flagged_not_posted(session, run) -> None:
    connection = await binance_connection(run)
    position = _discount(0, settleDate=None)

    await ingest_report(session, connection.id, "discount-positions", [position])
    report = await run_normalize(session, connection.id)

    assert await keys(session, connection.id) == []
    assert "discount-positions.settleDate missing" in report.new_flags


# Reconciliation: Spot and Earn with a consistent history of the family's records.


def _wallets(*wallets: tuple[str, list[tuple[str, str]]]) -> list[dict]:
    rows = []
    for name, held in wallets:
        listed = [
            {
                "asset": coin,
                "assetName": coin,
                "free": quantity,
                "locked": "0",
                "freeze": "0",
                "withdrawing": "0",
                "btcValuation": "0",
            }
            for coin, quantity in held
        ]
        balance = "1" if held else "0"
        rows.append(
            build_row("wallet-balance", walletName=name, balance=balance, assetBalances=listed)
        )
    return rows


def _spot(*held: tuple[str, str]) -> list[dict]:
    return [build_row("spot-account", asset=coin, free=q, locked="0") for coin, q in held]


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


async def test_spot_and_earn_reconcile_with_the_earn_products(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    spot_before = [("AAA", "20"), ("BFUSD", "51"), ("ETH", "2")]
    fake.set_rows("spot-account", _spot(*spot_before))
    fake.set_rows("wallet-balance", _wallets(("Spot", spot_before), ("Earn", [("AAA", "10")])))
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    start = clock.ms

    def hours(n: int) -> int:
        return start + n * HOUR_MS

    fake.add(
        "earn-locked-subscriptions",
        _locked_subscription(0, amount="4", time=hours(1)),
    )
    fake.add(
        "earn-locked-rewards",
        build_row("earn-locked-rewards", asset="AAA", amount="0.5", time=hours(2)),
    )
    fake.add(
        "earn-locked-redemptions",
        _locked_redemption(0, "MATURE", amount="2", time=hours(3)),
    )
    fake.add(
        "onchain-subscriptions",
        build_row(
            "onchain-subscriptions",
            asset="AAA",
            amount="1",
            status="SUCCESS",
            sourceAccount="SPOT",
            time=hours(3),
        ),
    )
    fake.add(
        "bfusd-redemptions",
        _swap_row(
            "bfusd-redemptions",
            ("BFUSD", "51"),
            ("STB", "50"),
            fee="1",
            time=hours(1),
            arrivalTime=hours(4),
        ),
    )
    fake.add("eth-stakes", _swap_row("eth-stakes", ("ETH", "2"), ("WBETH", "1.9"), time=hours(2)))
    fake.set_rows(
        "dci-positions",
        [
            _dci(
                0,
                investCoin="AAA",
                subscriptionAmount="3",
                subscriptionTime=hours(5),
                settleDate=hours(80),
            )
        ],
    )
    spot_after = [("AAA", "14"), ("STB", "50"), ("WBETH", "1.9")]
    fake.set_rows("spot-account", _spot(*spot_after))
    fake.set_rows("wallet-balance", _wallets(("Spot", spot_after), ("Earn", [("AAA", "16.5")])))
    notifier = RecordingNotifier()

    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert await _differences(session, connection.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    held = await holdings(session, connection.id)
    # Pending redemptions has no report: it nets to zero once the redemption arrived, and it
    # has no opening adjustment.
    assert not [h for h in held if h[0] == PENDING]
    live = await keys(session, connection.id)
    assert any(k.endswith(":arrival") for k in live)
    assert any(k.startswith("dci_position:") for k in live)
    assert not [k for k in live if k.startswith("opening:redeeming")]
    assert await find_transfer_violations(session) == []


async def test_a_report_row_is_its_own_typed_row(session, run, monkeypatch) -> None:
    # A position handler's `run.typed` reaches only the row it is applying, not the read's
    # other rows.
    connection = await binance_connection(run)
    first, second = _dci(0), _dci(1)
    moment = at(T + HOUR_MS)
    body = to_json(build_response("dci-positions", [first, second]))
    read = Read("dci-positions", body, moment, moment)
    await run(store_reports, connection, {"dci-positions": read})
    await ingest_report(session, connection.id, "dci-positions", [first, second], at_ms=T + HOUR_MS)

    async def mark(run_: Run, row: dict) -> None:
        assert run_.record.typed_key["id"] == row["id"]
        if row["id"] == first["id"]:
            await run_.typed(apr=Decimal(9))

    monkeypatch.setitem(normalize_module.POSITION_HANDLERS, "dci-positions", mark)
    await run_normalize(session, connection.id)

    rows = await typed(session, "binance_dci_positions", connection.id)
    assert {(r.id, r.apr) for r in rows} == {
        (first["id"], Decimal(9)),
        (second["id"], Decimal(second["apr"])),
    }
