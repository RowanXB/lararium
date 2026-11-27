"""What the connector reports instead of handling (design 5.13, 5.14): Portfolio Margin's balance
is counted and its records are not posted, while the classic margin and futures wallets it
replaces are not asked; Trading Bots and Copy Trading are counted from the wallet list, which
names no record of theirs; a symbol `exchangeInfo` no longer lists is named; and every new flag
of a sync is told once, together on one line."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from lararium.modules.finance.binance.balances import OWN_SOURCES, record_less_flag
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.fetch import (
    BALANCE_LABELS,
    NOT_PRESENT_KEY,
    PHASE_B_BUILT,
    PORTFOLIO_MARGIN_KEY,
    PORTFOLIO_MARGIN_PRODUCTS,
    PORTFOLIO_MARGIN_REPLACES,
    portfolio_margin_keys,
    walk_keys,
)
from lararium.modules.finance.binance.notices import FLAG_PREFIX
from lararium.modules.finance.binance.reconcile import READ_REPORTS
from lararium.modules.finance.binance.sync import PORTFOLIO_MARGIN_FLAG, delisted_flag

from .fixtures import HOUR_MS, NOW_MS, FakeBinance, build_row
from .normalizing import ingest, keys, typed
from .syncing import Clock, RecordingNotifier, binance_connection, later_config, run_binance_sync

PM = "Binance Portfolio Margin"
BOTS = "Binance Trading Bots"
UNIVERSAL = "universal-transfer"
_CHANGED = "changed by something no record explains"


def _pm_on(fake: FakeBinance, on: bool = True) -> None:
    fake.set_rows("account-info", [build_row("account-info", isPortfolioMarginRetailEnabled=on)])


def _pm_asset(
    code: str,
    total: str,
    um: str = "0",
    cm: str = "0",
    *,
    borrowed: str = "0",
    interest: str = "0",
) -> dict:
    return build_row(
        "pm-balance",
        asset=code,
        totalWalletBalance=total,
        umUnrealizedPNL=um,
        cmUnrealizedPNL=cm,
        crossMarginBorrowed=borrowed,
        crossMarginInterest=interest,
    )


def _cross(code: str, net: str) -> dict:
    return build_row(
        "margin-account",
        asset=code,
        free=net,
        locked="0",
        borrowed="0",
        interest="0",
        netAsset=net,
    )


def _um(code: str, wallet: str) -> dict:
    return build_row("um-account", asset=code, walletBalance=wallet, unrealizedProfit="0")


async def _held(session, connection_id, clock: Clock) -> dict[tuple[str, str], Decimal]:
    """Each Binance account's balance per asset tomorrow, as valuation counts it."""
    day = (datetime.fromtimestamp(clock.ms / 1000, UTC) + timedelta(days=1)).date()
    rows = await session.execute(
        text(
            "select a.name, i.symbol, b.quantity from finance.v1_balances_asof(:d) b"
            " join finance.accounts a on a.id = b.account_id"
            " join finance.instruments i on i.id = b.instrument_id"
            " where a.source_connection_id = :c"
        ),
        {"d": day, "c": connection_id},
    )
    return {(r.name, r.symbol): r.quantity for r in rows if r.quantity != 0}


def _listed(name: str, balance: str, *held: tuple[str, str]) -> dict:
    entries = [
        {
            "asset": code,
            "assetName": code,
            "free": free,
            "locked": "0",
            "freeze": "0",
            "withdrawing": "0",
            "btcValuation": "0",
        }
        for code, free in held
    ]
    return build_row("wallet-balance", walletName=name, balance=balance, assetBalances=entries)


async def _snapshots(session, connection_id, account: str) -> list[tuple]:
    rows = await session.execute(
        text(
            "select i.symbol, s.quantity, s.as_of from finance.balance_snapshots s"
            " join finance.accounts a on a.id = s.account_id"
            " join finance.instruments i on i.id = s.instrument_id"
            " where a.source_connection_id = :c and a.name = :n and s.kind = 'reported'"
            " order by s.as_of, i.symbol"
        ),
        {"c": connection_id, "n": account},
    )
    return [tuple(r) for r in rows]


async def _differences(session, connection_id) -> list[tuple]:
    rows = await session.execute(
        text(
            "select a.name, i.symbol from finance.binance_reconciliation_differences d"
            " join finance.accounts a on a.id = d.account_id"
            " join finance.instruments i on i.id = d.instrument_id"
            " where a.source_connection_id = :c and d.closed_at is null"
            " order by a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


def test_the_family_is_read_and_replaces_the_classic_wallets() -> None:
    assert set(PORTFOLIO_MARGIN_PRODUCTS) == {"pm-account", "pm-balance"}
    assert set(PORTFOLIO_MARGIN_PRODUCTS) <= set(PHASE_B_BUILT) & set(BALANCE_LABELS)
    assert OWN_SOURCES["portfolio_margin"] == "pm-balance"
    # Reconciled and opened on the wallet balance, as a futures wallet is (design 5.11).
    # Net of its debt, as Cross Margin's `netAsset` is (ruling R20).
    assert READ_REPORTS["portfolio_margin"] == (
        "pm-balance",
        (("totalWalletBalance", 1), ("crossMarginBorrowed", -1), ("crossMarginInterest", -1)),
    )
    # Portfolio Margin unifies Cross Margin and the futures wallets; Isolated Margin stays.
    assert {"margin-account", "um-account", "cm-account", "um-income", "cm-income"} <= set(
        PORTFOLIO_MARGIN_REPLACES
    )
    assert {"margin-trades", "um-trades", "cm-trades"} <= set(PORTFOLIO_MARGIN_REPLACES)
    assert "margin-isolated-account" not in PORTFOLIO_MARGIN_REPLACES
    # Each history's own keys are left out, and only those: an isolated pair's stay asked.
    own = {key for label in PORTFOLIO_MARGIN_REPLACES for key, _ in walk_keys(label)}
    assert set(portfolio_margin_keys()) == own | set(PORTFOLIO_MARGIN_REPLACES)
    assert "margin-transfers" in portfolio_margin_keys()
    for label in PORTFOLIO_MARGIN_REPLACES:
        assert ENDPOINTS[label].limit in ("ip", "uid", "fapi", "dapi"), label


async def test_portfolio_margin_is_counted_and_flagged(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pm_on(fake)
    fake.set_rows(
        "pm-balance", [_pm_asset("STB", "100", um="2.5", cm="-0.5"), _pm_asset("AAA", "1")]
    )
    # A dividend Binance did not serve: Binance's own word, kept.
    connection = await binance_connection(
        run, config={**later_config(), NOT_PRESENT_KEY: {"dividends": NOW_MS - HOUR_MS}}
    )
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    # Each asset's wallet balance plus its unrealised futures profit.
    shown = await _snapshots(session, after.id, PM)
    assert [(code, quantity) for code, quantity, _ in shown] == [
        ("AAA", Decimal(1)),
        ("STB", Decimal(102)),
    ]
    # Cut when the balance was asked, as every wallet's report is.
    assert len({as_of for _, _, as_of in shown}) == 1
    assert PORTFOLIO_MARGIN_FLAG in after.config["flags"]
    assert any(PORTFOLIO_MARGIN_FLAG in m for m in notifier.messages)
    # Its records are not posted: nothing is on its account but its opening.
    posted = [k for k in await keys(session, after.id) if "portfolio" in k]
    assert posted and all(k.startswith("opening:portfolio_margin:") for k in posted)
    assert len(await typed(session, "binance_pm_balance", after.id)) == 2
    assert len(await typed(session, "binance_pm_account", after.id)) == 1
    # The wallets it replaces are not asked, and wait in `not_present` while it is on.
    for label in PORTFOLIO_MARGIN_REPLACES:
        assert fake.calls(label) == [], label
    missing = after.config[NOT_PRESENT_KEY]
    assert set(portfolio_margin_keys()) <= set(missing)  # pyright: ignore[reportArgumentType]
    assert isinstance(after.config[PORTFOLIO_MARGIN_KEY], int)
    # Isolated Margin is not one of them; the derivatives hosts' key-type refusals are not used.
    assert fake.calls("margin-isolated-account")
    assert "refused_hosts" not in after.config or after.config["refused_hosts"] == {}

    # Still on later: still not asked, and told once.
    clock.ms += 5 * HOUR_MS
    again = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    assert again.status == "healthy", again.last_error
    for label in PORTFOLIO_MARGIN_REPLACES:
        assert fake.calls(label) == [], label
    assert sum(PORTFOLIO_MARGIN_FLAG in m for m in notifier.messages) == 1


async def test_portfolio_margin_switched_off_asks_the_classic_wallets_at_once(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pm_on(fake)
    fake.set_rows("pm-balance", [_pm_asset("STB", "10")])
    connection = await binance_connection(
        run, config={**later_config(), NOT_PRESENT_KEY: {"dividends": NOW_MS - HOUR_MS}}
    )
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error
    asked = len(fake.calls("pm-balance"))

    _pm_on(fake, on=False)
    clock.ms += 5 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    # Not 7 days later: the sync that sees it off asks them again.
    for label in ("margin-account", "um-account", "cm-account", "um-income", "margin-transfers"):
        assert fake.calls(label), label
    assert PORTFOLIO_MARGIN_KEY not in after.config
    missing = after.config[NOT_PRESENT_KEY]
    assert isinstance(missing, dict)
    # What Binance itself said stays; the margin account the fake has not opened is not present
    # by its own answer again.
    assert missing["dividends"] == NOW_MS - HOUR_MS
    assert "um-account" not in missing and "um-income" not in missing
    assert "margin-account" in missing
    # Portfolio Margin is not read while off.
    assert len(fake.calls("pm-balance")) == asked


async def test_portfolio_margin_reconciles_on_its_wallet_balance(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pm_on(fake)
    fake.set_rows("spot-account", [build_row("spot-account", asset="STB", free="100", locked="0")])
    fake.set_rows("pm-balance", [_pm_asset("STB", "100", um="5")])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error
    start = clock.ms

    # 50 from Spot into Portfolio Margin; the unrealised profit moves from 5 to -2.
    kind = "MAIN_PORTFOLIO_MARGIN"
    fake.add(
        UNIVERSAL,
        build_row(
            UNIVERSAL,
            0,
            asset="STB",
            amount="50",
            type=kind,
            status="CONFIRMED",
            timestamp=start + HOUR_MS,
        ),
        when={"type": kind},
    )
    fake.set_rows("spot-account", [build_row("spot-account", asset="STB", free="50", locked="0")])
    fake.set_rows("pm-balance", [_pm_asset("STB", "150", um="-2")])
    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert await _differences(session, after.id) == []
    assert [q for _, q, _ in await _snapshots(session, after.id, PM)] == [
        Decimal(105),
        Decimal(148),
    ]

    # A realised profit: no record of it is posted, so the wallet balance's change is one.
    fake.set_rows("pm-balance", [_pm_asset("STB", "151", um="-2")])
    clock.ms += 6 * HOUR_MS
    later = await run_binance_sync(session, connection.id, fake.handler)
    assert later.status == "healthy", later.last_error
    assert await _differences(session, later.id) == [(PM, "STB")]


async def test_record_less_wallet_is_counted_and_flagged_once(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows(
        "wallet-balance",
        [_listed("Trading Bots", "1", ("AAA", "3")), _listed("Copy Trading", "0")],
    )
    connection = await binance_connection(run, config=later_config())
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    [(code, quantity, _)] = await _snapshots(session, after.id, BOTS)
    assert (code, quantity) == ("AAA", Decimal(3))
    bots, copy = record_less_flag("Trading Bots"), record_less_flag("Copy Trading")
    assert bots == "Trading Bots has a balance: Binance lists no records for it"
    flags = after.config["flags"]
    assert isinstance(flags, list)
    assert bots in flags and copy not in flags

    fake.set_rows(
        "wallet-balance",
        [_listed("Trading Bots", "1", ("AAA", "4")), _listed("Copy Trading", "1", ("BBB", "2"))],
    )
    clock.ms += 5 * HOUR_MS
    again = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert again.status == "healthy", again.last_error
    told = [m for m in notifier.messages if FLAG_PREFIX in m]
    assert sum(bots in m for m in told) == 1
    assert sum(copy in m for m in told) == 1
    assert [q for _, q, _ in await _snapshots(session, again.id, BOTS)] == [Decimal(3), Decimal(4)]


async def test_a_record_less_wallet_reconciles_with_a_consistent_history(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.set_rows("wallet-balance", [_listed("Trading Bots", "1", ("AAA", "3"))])
    connection = await binance_connection(run, config=later_config())

    # No record moves it (Binance lists none, the universal transfer types name no bot wallet):
    # a balance that stays is consistent, whatever the number of reports.
    for _ in range(3):
        after = await run_binance_sync(session, connection.id, fake.handler)
        assert after.status == "healthy", after.last_error
        clock.ms += 5 * HOUR_MS
    assert await _differences(session, after.id) == []
    assert not [w for w in after.warnings if _CHANGED in w]
    # Its first report is its opening.
    assert [k for k in await keys(session, after.id) if k.startswith("opening:trading_bots:")]

    # What it earns or loses shows as a difference, named and never corrected.
    fake.set_rows("wallet-balance", [_listed("Trading Bots", "1", ("AAA", "3.5"))])
    changed = await run_binance_sync(session, connection.id, fake.handler)
    assert changed.status == "healthy", changed.last_error
    assert await _differences(session, changed.id) == [(BOTS, "AAA")]
    assert [w for w in changed.warnings if w.startswith(f"{BOTS}: AAA {_CHANGED}")]


async def test_delisted_symbol_is_flagged(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    listed = build_row("exchange-info", symbol="AAABBB", baseAsset="AAA", quoteAsset="BBB")
    old = build_row("exchange-info", symbol="OLDBBB", baseAsset="OLD", quoteAsset="BBB")
    margin = build_row("exchange-info", symbol="GONBBB", baseAsset="GON", quoteAsset="BBB")
    fake.set_rows("exchange-info", [listed, old, margin])
    connection = await binance_connection(run, config=later_config())
    traded = NOW_MS - 2 * HOUR_MS
    for symbol in ("AAABBB", "OLDBBB"):
        trade = build_row("my-trades", symbol=symbol, time=traded, commission="0", quoteQty="2.25")
        await ingest(session, connection.id, "my-trades", [trade])
    cross = build_row("margin-trades", symbol="GONBBB", time=traded, commission="0")
    isolated = build_row(
        "margin-trades", 1, symbol="ISOBBB", time=traded, commission="0", isIsolated=True
    )
    await ingest(session, connection.id, "margin-trades", [{**cross, "isIsolated": False}])
    await ingest(session, connection.id, "margin-trades", [isolated])

    notifier = RecordingNotifier()
    first = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    assert first.status == "healthy", first.last_error
    assert not [f for f in first.config["flags"] if "no longer listed" in f]  # pyright: ignore[reportGeneralTypeIssues]

    # Binance delists two of them: their trades cannot be fetched any more.
    fake.set_rows("exchange-info", [listed])
    clock.ms += 5 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    flags = after.config["flags"]
    assert isinstance(flags, list)
    assert delisted_flag("OLDBBB") == "OLDBBB is no longer listed: its trades cannot be fetched"
    assert [f for f in flags if isinstance(f, str) and "no longer listed" in f] == [
        delisted_flag("GONBBB"),
        delisted_flag("OLDBBB"),
    ]
    # Its trades stored before stay posted: the latest listing of it gives its pair.
    assert "spot_trade:OLDBBB:" + str(build_row("my-trades")["id"]) in await keys(session, after.id)
    told = [m for m in notifier.messages if "no longer listed" in m]
    assert len(told) == 1

    # Still delisted: flagged once, so a later sync tells nothing of it again.
    clock.ms += 5 * HOUR_MS
    quiet = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    assert quiet.status == "healthy", quiet.last_error
    assert [m for m in notifier.messages if "no longer listed" in m] == told


async def test_flags_are_notified_once_on_one_line(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pm_on(fake)
    fake.set_rows("pm-balance", [_pm_asset("STB", "10")])
    fake.set_rows("wallet-balance", [_listed("Trading Bots", "1", ("AAA", "3"))])
    at_ms = NOW_MS - 2 * HOUR_MS
    deposit = build_row(
        "deposits", status=1, walletType=0, insertTime=at_ms, completeTime=at_ms, coin="AAA"
    )
    fake.add("deposits", {**deposit, "extraField": "secret-value-4242"})
    connection = await binance_connection(run, config=later_config())
    trade = build_row("my-trades", symbol="OLDBBB", time=at_ms, commission="0")
    await ingest(session, connection.id, "my-trades", [trade])
    notifier = RecordingNotifier()

    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    expected = {
        PORTFOLIO_MARGIN_FLAG,
        record_less_flag("Trading Bots"),
        delisted_flag("OLDBBB"),
        "deposits.extraField (a field the catalogue does not list)",
    }
    flags = after.config["flags"]
    assert isinstance(flags, list)
    assert expected <= set(flags)
    [message] = notifier.messages
    [line] = [line for line in message.splitlines() if FLAG_PREFIX in line]
    # Every new flag of the sync, whichever step raised it, on that one line and on no other.
    assert all(flag in line for flag in flags if isinstance(flag, str))
    other = [line for line in message.splitlines() if FLAG_PREFIX not in line]
    assert not [flag for flag in expected if any(flag in o for o in other)]
    assert "secret-value" not in message

    clock.ms += 5 * HOUR_MS
    again = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)
    assert again.status == "healthy", again.last_error
    assert len([m for m in notifier.messages if FLAG_PREFIX in m]) == 1
    assert "pending_flags" not in again.config


async def test_portfolio_margin_is_counted_net_of_its_debt(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    _pm_on(fake)
    fake.set_rows("pm-balance", [_pm_asset("STB", "100", um="2", borrowed="10", interest="0.5")])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    # A borrowed asset is negative, as in Cross Margin: 100 - 10 - 0.5 + 2.
    assert [q for _, q, _ in await _snapshots(session, first.id, PM)] == [Decimal("91.5")]

    # Interest accrues: no record of it is posted, so the net wallet balance's change is a
    # difference; the unrealised profit's change alone is not one.
    fake.set_rows("pm-balance", [_pm_asset("STB", "100", um="7", borrowed="10", interest="0.5")])
    clock.ms += 5 * HOUR_MS
    moved = await run_binance_sync(session, connection.id, fake.handler)
    assert moved.status == "healthy", moved.last_error
    assert await _differences(session, moved.id) == []
    fake.set_rows("pm-balance", [_pm_asset("STB", "100", um="7", borrowed="10", interest="0.75")])
    clock.ms += 5 * HOUR_MS
    accrued = await run_binance_sync(session, connection.id, fake.handler)
    assert accrued.status == "healthy", accrued.last_error
    assert await _differences(session, accrued.id) == [(PM, "STB")]
    # The opening is the net wallet balance before any record: 100 - 10 - 0.5.
    [opening] = [k for k in await keys(session, accrued.id) if k.startswith("opening:portfolio")]
    rows = await session.execute(
        text(
            "select p.quantity from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " join finance.accounts a on a.id = p.account_id"
            " where t.external_id = :e and a.name = :n"
        ),
        {"e": opening, "n": PM},
    )
    assert [r.quantity for r in rows] == [Decimal("89.5")]


async def test_switching_portfolio_margin_never_counts_an_asset_twice(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    # Before Portfolio Margin: 10 STB on Cross Margin, 5 on USDⓈ-M Futures.
    fake.margin = True
    fake.set_rows("margin-account", [_cross("STB", "10")])
    fake.set_rows("um-account", [_um("STB", "5")])
    connection = await binance_connection(run, config=later_config())
    before = await run_binance_sync(session, connection.id, fake.handler)
    assert before.status == "healthy", before.last_error
    assert await _held(session, before.id, clock) == {
        ("Binance Cross Margin", "STB"): Decimal(10),
        ("Binance USDⓈ-M Futures", "STB"): Decimal(5),
    }

    # Switched on: Portfolio Margin holds the same 15; the wallets it replaces report nothing.
    _pm_on(fake)
    fake.set_rows("pm-balance", [_pm_asset("STB", "15")])
    clock.ms += 5 * HOUR_MS
    on = await run_binance_sync(session, connection.id, fake.handler)
    assert on.status == "healthy", on.last_error
    assert await _held(session, on.id, clock) == {(PM, "STB"): Decimal(15)}
    for name in ("Binance Cross Margin", "Binance USDⓈ-M Futures"):
        latest = (await _snapshots(session, on.id, name))[-1]
        assert latest[:2] == ("STB", Decimal(0)), name
    # The switch shows: the replaced wallets' change is unexplained, Portfolio Margin's first
    # report is its opening.
    assert await _differences(session, on.id) == [
        ("Binance Cross Margin", "STB"),
        ("Binance USDⓈ-M Futures", "STB"),
    ]

    # Switched off: the classic wallets hold it again; Portfolio Margin reports nothing.
    _pm_on(fake, on=False)
    clock.ms += 5 * HOUR_MS
    off = await run_binance_sync(session, connection.id, fake.handler)
    assert off.status == "healthy", off.last_error
    assert await _held(session, off.id, clock) == {
        ("Binance Cross Margin", "STB"): Decimal(10),
        ("Binance USDⓈ-M Futures", "STB"): Decimal(5),
    }
    assert (await _snapshots(session, off.id, PM))[-1][:2] == ("STB", Decimal(0))
    assert (PM, "STB") in await _differences(session, off.id)
