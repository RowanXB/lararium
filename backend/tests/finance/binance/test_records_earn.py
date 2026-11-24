"""Flexible Simple Earn in the ledger (design 5.7): moves between wallets as one transaction,
rewards as income on Earn, and a reward paid out as an asset dividend posted once, on Spot
(rule 3)."""

from decimal import Decimal

import pytest

from .fixtures import BASE_TIME_MS, HOUR_MS, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    keys,
    ledger,
    normalize_rows,
    plain,
    postings,
    run_normalize,
    typed,
)
from .syncing import binance_connection

SUBSCRIPTIONS = "earn-flexible-subscriptions"
REDEMPTIONS = "earn-flexible-redemptions"
REWARDS = "earn-flexible-rewards"


def _subscription(seq: int = 0, **fields) -> dict:
    values = {"asset": "AAA", "amount": "4", "status": "SUCCESS", "sourceAccount": "SPOT"}
    return build_row(SUBSCRIPTIONS, seq, **(values | fields))


def _redemption(seq: int = 0, **fields) -> dict:
    values = {"asset": "AAA", "amount": "3", "status": "PAID", "destAccount": "SPOT"}
    return build_row(REDEMPTIONS, seq, **(values | fields))


def _reward(seq: int = 0, **fields) -> dict:
    values = {"asset": "AAA", "rewards": "0.25", "type": "REALTIME"}
    return build_row(REWARDS, seq, **(values | fields))


async def test_subscription_moves_between_wallets(session, run) -> None:
    connection = await binance_connection(run)
    from_spot = _subscription(0)
    from_funding = _subscription(1, sourceAccount="FUNDING")
    from_both = _subscription(
        2, sourceAccount="SPOTANDFUNDING", amtFromSpot="1", amtFromFunding="3"
    )
    rows = [from_spot, from_funding, from_both]

    await normalize_rows(session, connection.id, (SUBSCRIPTIONS, rows))

    def key(row: dict) -> str:
        return f"earn_flexible_subscription:{row['purchaseId']}"

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
    # One record describes both sides: nothing waits in transit.
    assert ("In transit", "AAA") not in await holdings(session, connection.id)
    await assert_balanced_and_quiet(session, connection.id)


@pytest.mark.parametrize("status", ["PURCHASING", "FAILED"])
async def test_unfinished_subscription_is_not_posted(session, run, status: str) -> None:
    connection = await binance_connection(run)

    await normalize_rows(session, connection.id, (SUBSCRIPTIONS, [_subscription(status=status)]))

    assert await keys(session, connection.id) == []
    assert len(await typed(session, "binance_earn_flexible_subscriptions", connection.id)) == 1


async def test_redemption_returns_to_its_destination(session, run) -> None:
    connection = await binance_connection(run)
    to_spot, to_funding = _redemption(0), _redemption(1, destAccount="FUNDING")
    pending = _redemption(2, status="CREATED")

    await normalize_rows(session, connection.id, (REDEMPTIONS, [to_spot, to_funding, pending]))

    assert plain(await postings(session, f"earn_flexible_redemption:{to_spot['redeemId']}")) == [
        ("Binance Earn", "AAA", Decimal(-3)),
        ("Binance Spot", "AAA", Decimal(3)),
    ]
    assert plain(await postings(session, f"earn_flexible_redemption:{to_funding['redeemId']}")) == [
        ("Binance Earn", "AAA", Decimal(-3)),
        ("Binance Funding", "AAA", Decimal(3)),
    ]
    assert f"earn_flexible_redemption:{pending['redeemId']}" not in await keys(
        session, connection.id
    )
    await assert_balanced_and_quiet(session, connection.id)


async def test_realtime_reward_accrues_in_earn(session, run) -> None:
    connection = await binance_connection(run)
    reward = _reward()

    await normalize_rows(session, connection.id, (REWARDS, [reward]))

    [key] = await keys(session, connection.id)
    assert key.startswith("earn_flexible_reward:") and key.endswith(":1")
    assert plain(await postings(session, key)) == [
        ("Binance Earn", "AAA", Decimal("0.25")),
        ("Unassigned", "AAA", Decimal("-0.25")),
    ]
    [row] = await typed(session, "binance_earn_flexible_rewards", connection.id)
    assert row.transaction_id is not None and row.dividend_id is None
    await assert_balanced_and_quiet(session, connection.id)


async def test_two_identical_rewards_are_two_records(session, run) -> None:
    connection = await binance_connection(run)
    reward = _reward()

    await normalize_rows(session, connection.id, (REWARDS, [reward, reward]))

    assert len(await typed(session, "binance_earn_flexible_rewards", connection.id)) == 2
    assert [k.rsplit(":", 1)[1] for k in await keys(session, connection.id)] == ["1", "2"]
    assert (await holdings(session, connection.id))[("Binance Earn", "AAA")] == Decimal("0.5")
    await assert_balanced_and_quiet(session, connection.id)


def _payout() -> tuple[dict, dict, dict]:
    reward = _reward(type="BONUS", rewards="0.7", time=BASE_TIME_MS)
    dividend = build_row(
        "dividends", asset="AAA", amount="0.7", direction=1, divTime=BASE_TIME_MS + 3 * HOUR_MS
    )
    subscription = _subscription(
        type="AUTO", amount="0.7", time=BASE_TIME_MS + 4 * HOUR_MS, sourceAccount="SPOT"
    )
    return reward, dividend, subscription


# How the reward, the dividend and the subscription reach the normaliser: batches per run.
_ARRIVALS = {
    "reward first": [[REWARDS, "dividends", SUBSCRIPTIONS]],
    "dividend first": [["dividends", REWARDS, SUBSCRIPTIONS]],
    "reward, then the rest": [[REWARDS], ["dividends", SUBSCRIPTIONS]],
    "dividend, then the rest": [["dividends"], [REWARDS, SUBSCRIPTIONS]],
}


@pytest.mark.parametrize("arrival", list(_ARRIVALS))
async def test_bonus_reward_is_its_dividend(session, run, arrival: str) -> None:
    connection = await binance_connection(run)
    reward, dividend, subscription = _payout()
    rows = {REWARDS: reward, "dividends": dividend, SUBSCRIPTIONS: subscription}

    for labels in _ARRIVALS[arrival]:
        for label in labels:
            await ingest(session, connection.id, label, [rows[label]])
        await run_normalize(session, connection.id)

    assert await holdings(session, connection.id) == {
        ("Binance Earn", "AAA"): Decimal("0.7"),
        ("Unassigned", "AAA"): Decimal("-0.7"),
    }
    assert plain(await postings(session, f"dividend:{dividend['id']}"))[0] == (
        "Binance Spot",
        "AAA",
        Decimal("0.7"),
    )
    live = await keys(session, connection.id)
    assert not [k for k in live if k.startswith("earn_flexible_reward")]
    [row] = await typed(session, "binance_earn_flexible_rewards", connection.id)
    assert (row.dividend_id, row.transaction_id) == (str(dividend["id"]), None)
    found = [(k, a, s, q) for k, a, s, q, *_ in await ledger(session, connection.id)]
    moved = f"earn_flexible_subscription:{subscription['purchaseId']}"
    assert found == [
        (f"dividend:{dividend['id']}", "Binance Spot", "AAA", Decimal("0.7")),
        (f"dividend:{dividend['id']}", "Unassigned", "AAA", Decimal("-0.7")),
        (moved, "Binance Spot", "AAA", Decimal("-0.7")),
        (moved, "Binance Earn", "AAA", Decimal("0.7")),
    ]
    await assert_balanced_and_quiet(session, connection.id)


async def test_reward_is_linked_to_the_nearest_dividend_within_a_day_and_two_hours(
    session, run
) -> None:
    connection = await binance_connection(run)
    reward = _reward(type="BONUS", rewards="0.7", time=BASE_TIME_MS)

    def dividend(seq: int, hours: int) -> dict:
        moment = BASE_TIME_MS + hours * HOUR_MS
        return build_row("dividends", seq, asset="AAA", amount="0.7", direction=1, divTime=moment)

    too_late, far, near = dividend(0, 27), dividend(1, -20), dividend(2, 5)

    await normalize_rows(
        session, connection.id, (REWARDS, [reward]), ("dividends", [too_late, far, near])
    )

    [row] = await typed(session, "binance_earn_flexible_rewards", connection.id)
    assert row.dividend_id == str(near["id"])
    await assert_balanced_and_quiet(session, connection.id)


async def test_reward_with_no_dividend_of_its_amount_stays_posted(session, run) -> None:
    connection = await binance_connection(run)
    reward = _reward(type="BONUS", rewards="0.7")
    other = build_row("dividends", asset="AAA", amount="0.8", direction=1)

    await normalize_rows(session, connection.id, (REWARDS, [reward]), ("dividends", [other]))

    [row] = await typed(session, "binance_earn_flexible_rewards", connection.id)
    assert row.dividend_id is None and row.transaction_id is not None


async def test_a_realtime_reward_is_never_a_dividend(session, run) -> None:
    # A REALTIME reward accrues inside the Earn position and is never paid out: a dividend of
    # the same asset and amount an hour later is another income.
    connection = await binance_connection(run)
    reward = _reward(rewards="0.7", time=BASE_TIME_MS)
    dividend = build_row(
        "dividends", asset="AAA", amount="0.7", direction=1, divTime=BASE_TIME_MS + HOUR_MS
    )

    await normalize_rows(session, connection.id, (REWARDS, [reward]), ("dividends", [dividend]))

    [row] = await typed(session, "binance_earn_flexible_rewards", connection.id)
    assert row.dividend_id is None and row.transaction_id is not None
    assert (await holdings(session, connection.id))[("Binance Earn", "AAA")] == Decimal("0.7")
    assert plain(await postings(session, f"dividend:{dividend['id']}"))[0] == (
        "Binance Spot",
        "AAA",
        Decimal("0.7"),
    )
    await assert_balanced_and_quiet(session, connection.id)


def _bonus(seq: int, hours: int) -> dict:
    return _reward(seq, type="BONUS", rewards="0.7", time=BASE_TIME_MS + hours * HOUR_MS)


def _dividend_at(hours: int) -> dict:
    moment = BASE_TIME_MS + hours * HOUR_MS
    return build_row("dividends", asset="AAA", amount="0.7", direction=1, divTime=moment)


async def _links(session, connection_id) -> dict[str, str | None]:
    return {
        str(row.time): row.dividend_id
        for row in await typed(session, "binance_earn_flexible_rewards", connection_id)
    }


# The dividend is at 3 h; the reward that comes second is older (-1 h, farther) or nearer (2 h).
@pytest.mark.parametrize("second_hours", [-1, 2])
async def test_links_do_not_depend_on_how_runs_split_the_records(
    session, run, second_hours: int
) -> None:
    first, second, dividend = _bonus(0, 0), _bonus(1, second_hours), _dividend_at(3)
    together = await binance_connection(run)
    await normalize_rows(
        session, together.id, (REWARDS, [first, second]), ("dividends", [dividend])
    )
    split = await binance_connection(run)
    await normalize_rows(session, split.id, (REWARDS, [first]), ("dividends", [dividend]))
    await normalize_rows(session, split.id, (REWARDS, [second]))

    expected = await _links(session, together.id)
    nearest = str(at(BASE_TIME_MS + max(0, second_hours) * HOUR_MS))
    assert expected[nearest] == str(dividend["id"])
    assert list(expected.values()).count(None) == 1
    assert await _links(session, split.id) == expected
    assert await ledger(session, split.id) == await ledger(session, together.id)
    await run_normalize(session, split.id, again=True)
    assert await _links(session, split.id) == expected
    assert await ledger(session, split.id) == await ledger(session, together.id)
    await assert_balanced_and_quiet(session, split.id)
