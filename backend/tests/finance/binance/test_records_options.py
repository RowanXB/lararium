"""The Options wallet in the ledger (design 5.5, 5.6, 5.7, 5.11): reported from the margin
account's `asset[]` as each asset's equity, and reconciled (and given its opening) on the margin
balance alone; every bill row but a transfer is an amount against Unassigned, a fee paid is a
fee, a transfer is a movement the universal transfer history may list first (rule 2), and
trades, exercises and positions are typed only. The options host's own exchange information
sets its request limit. An Ed25519 key a futures or options host refuses leaves that host out
and says so, without rejecting the connection (design 5.1)."""

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from lararium.core.commands import command_context
from lararium.core.connections import store_credential, update_config
from lararium.modules.finance.binance.balances import (
    OPTIONS_SOURCES,
    OWN_SOURCES,
    Read,
    report_balances,
)
from lararium.modules.finance.binance.catalogue import ENDPOINTS
from lararium.modules.finance.binance.client import host_refused_flag, key_type_flag
from lararium.modules.finance.binance.fetch import (
    BALANCE_LABELS,
    CURSORS_KEY,
    HISTORY_LABELS,
    LIMITS_INFO,
    NOT_PRESENT_KEY,
    PHASE_B_BUILT,
    REFUSED_HOSTS_KEY,
    SYMBOL_TRADES,
    TRADE_LABELS,
    to_json,
)
from lararium.modules.finance.binance.normalize import HANDLERS, MOVEMENT_LISTINGS, Run
from lararium.modules.finance.binance.opening import set_openings
from lararium.modules.finance.binance.parse import BinanceDataError
from lararium.modules.finance.binance.reconcile import check
from lararium.modules.finance.binance.records.options import BILL_FEES, TRANSFER, bill_types
from lararium.modules.finance.binance.sync import limits_unread_flag, read_limits

from .fixtures import BASE_TIME_MS, DAY_MS, HOUR_MS, FakeBinance, build_response, build_row
from .normalizing import (
    assert_balanced_and_quiet,
    at,
    holdings,
    ingest,
    keys,
    normalize_rows,
    plain,
    postings,
    transaction,
    typed,
)
from .syncing import (
    API_SECRET,
    CONNECTOR,
    KEY,
    Clock,
    RecordingNotifier,
    binance_connection,
    client_for,
    context,
    later_config,
    run_binance_sync,
)

T = BASE_TIME_MS
BILL = "opt-bill"
ACCOUNT = "opt-account"
NAME = "Binance Options"
SPOT = "Binance Spot"
UNIVERSAL = "universal-transfer"
SYMBOL = "AAA-261231-100-C"
_CHANGED = "changed by something no record explains"
_FAMILY = {BILL, "opt-exercises", ACCOUNT, "opt-positions"}


def _asset(code: str, margin: str, equity: str | None) -> dict:
    return build_row(ACCOUNT, asset=code, marginBalance=margin, equity=equity)


def _read(label: str, rows: list, at_ms: int) -> Read:
    body = build_response(label, rows)
    if isinstance(body, dict) and "greek" in body:
        body = {**body, "greek": []}
    return Read(label, to_json(body), at(at_ms), at(at_ms + 200))


def _bill(seq: int, kind: str, amount: str, **fields) -> dict:
    values = {
        "type": kind,
        "amount": amount,
        "asset": "STB",
        "symbol": SYMBOL,
        "createDate": T + seq * HOUR_MS,
    }
    row = build_row(BILL, seq, **(values | fields))
    if kind == TRANSFER:
        # "omitted for other types such as TRANSFER".
        del row["symbol"]
    return row


def _key(row: dict) -> str:
    return f"opt_bill:{row['id']}"


async def _snapshots(session, connection_id) -> list[tuple]:
    rows = await session.execute(
        text(
            "select a.name, i.symbol, s.quantity, s.as_of from finance.balance_snapshots s"
            " join finance.accounts a on a.id = s.account_id"
            " join finance.instruments i on i.id = s.instrument_id"
            " where a.source_connection_id = :c and s.kind = 'reported'"
            " order by s.as_of, a.name, i.symbol"
        ),
        {"c": connection_id},
    )
    return [tuple(r) for r in rows]


async def _never_transfer(session, key: str) -> bool:
    return await session.scalar(
        text("select never_transfer from finance.transactions where external_id = :e"), {"e": key}
    )


def test_the_family_is_walked_and_read() -> None:
    assert _FAMILY <= set(PHASE_B_BUILT)
    assert {BILL, "opt-exercises"} <= set(HISTORY_LABELS)
    assert {ACCOUNT, "opt-positions"} <= set(BALANCE_LABELS)
    # Trades are walked per symbol the bill rows name; the exchange information is read first.
    assert SYMBOL_TRADES["opt-trades"] == BILL
    assert "opt-exchange-info" in LIMITS_INFO
    for label in ("opt-trades", "opt-exchange-info"):
        assert label not in HISTORY_LABELS and label not in BALANCE_LABELS
        assert label not in TRADE_LABELS
    assert OPTIONS_SOURCES == {"options": ACCOUNT}
    assert set(OPTIONS_SOURCES.items()) <= set(OWN_SOURCES.items())
    assert BILL in HANDLERS
    assert [listing.label for listing in MOVEMENT_LISTINGS][-1] == BILL


# The reported balance.


async def test_reported_balance_is_the_equity(session, run) -> None:
    connection = await binance_connection(run)
    flags: list[str] = []
    rows = [_asset("STB", "100", "97.5"), _asset("AAA", "1", "1.25")]
    listed = [build_row("wallet-balance", walletName="Options", balance="1", assetBalances=[])]
    reads = {
        ACCOUNT: _read(ACCOUNT, rows, T),
        "wallet-balance": _read("wallet-balance", listed, T + 1_000),
    }

    await run(report_balances, connection, reads, flag=flags.append)

    assert await _snapshots(session, connection.id) == [
        (NAME, "AAA", Decimal("1.25"), at(T)),
        (NAME, "STB", Decimal("97.5"), at(T)),
    ]
    # The wallet list agrees with the account's own report, and is not counted again.
    assert flags == []
    stored = await typed(session, "binance_opt_account_asset", connection.id)
    assert sorted((r.asset, r.margin_balance, r.equity) for r in stored) == [
        ("AAA", Decimal(1), Decimal("1.25")),
        ("STB", Decimal(100), Decimal("97.5")),
    ]

    # Neither the equity nor the margin balance reconciliation compares is ever read as 0.
    for unstated, field in (
        (_asset("STB", "100", None), "asset[].equity"),
        (build_row(ACCOUNT, asset="STB", marginBalance=None, equity="1"), "asset[].marginBalance"),
    ):
        with pytest.raises(BinanceDataError) as caught:
            await run(
                report_balances, connection, {ACCOUNT: _read(ACCOUNT, [unstated], T + DAY_MS)}
            )
        assert (caught.value.label, caught.value.field) == (ACCOUNT, field)


async def test_reconciliation_uses_the_margin_balance(session, run) -> None:
    connection = await binance_connection(run)
    await run(
        report_balances, connection, {ACCOUNT: _read(ACCOUNT, [_asset("STB", "100", "105")], T)}
    )
    rows = [
        _bill(1, "CONTRACT", "-10"),
        _bill(2, "FEE", "-0.5"),
        _bill(3, "EXERCISE_PNL", "3"),
    ]
    await normalize_rows(session, connection.id, (BILL, rows))
    # The margin balance moved by what the bill says (-7.5); the equity also by the positions'
    # unrealised value, from +5 to +12.
    later = [_asset("STB", "92.5", "104.5")]
    await run(report_balances, connection, {ACCOUNT: _read(ACCOUNT, later, T + 4 * HOUR_MS)})

    assert [q for _, _, q, _ in await _snapshots(session, connection.id)] == [
        Decimal(105),
        Decimal("104.5"),
    ]
    assert await check(session, connection.id, ()) == []

    # A margin balance the records do not explain is a difference of its change.
    unexplained = [_asset("STB", "93.5", "104.5")]
    await run(report_balances, connection, {ACCOUNT: _read(ACCOUNT, unexplained, T + 5 * HOUR_MS)})
    [difference] = await check(session, connection.id, ())
    assert (difference.reported_change, difference.posted_sum) == (Decimal(1), Decimal(0))
    assert difference.from_at == at(T + 4 * HOUR_MS)


# The bill.


async def test_bill_types_cover_the_catalogue_enum(session, run) -> None:
    connection = await binance_connection(run)
    documented = bill_types()
    assert documented == {"FEE", "CONTRACT", "EXERCISE_PNL", "EXERCISE_FEE", TRANSFER}
    assert BILL_FEES <= documented
    kinds = sorted(documented - {TRANSFER})
    paid = [_bill(2 * n, kind, "-1.5") for n, kind in enumerate(kinds)]
    received = [_bill(2 * n + 1, kind, "2") for n, kind in enumerate(kinds)]
    unknown = _bill(999, "SOMETHING_NEW", "4")

    report = await normalize_rows(session, connection.id, (BILL, [*paid, *received, unknown]))

    for row in paid:
        memo = row["type"].lower().replace("_", " ")
        fee = row["type"] in BILL_FEES
        assert await postings(session, _key(row)) == [
            (NAME, "STB", Decimal("-1.5"), None, None, None, None, fee, memo),
            ("Unassigned", "STB", Decimal("1.5"), None, None, None, None, False, ""),
        ], row["type"]
    for row in received:
        assert plain(await postings(session, _key(row))) == [
            (NAME, "STB", Decimal(2)),
            ("Unassigned", "STB", Decimal(-2)),
        ], row["type"]
        assert await _never_transfer(session, _key(row)) is True
    # Premiums, exercise profit and fees state no income kind.
    for row in [*paid, *received]:
        assert (await transaction(session, _key(row))).income_kind is None, row["type"]
    # A type the catalogue does not list is flagged by name and not posted.
    assert len(await keys(session, connection.id)) == 2 * len(kinds)
    assert f"{BILL}.type=SOMETHING_NEW" in report.new_flags
    stored = await typed(session, "binance_opt_bill", connection.id)
    assert [r.transaction_id for r in stored if r.type == "SOMETHING_NEW"] == [None]
    await assert_balanced_and_quiet(session, connection.id)


@pytest.mark.parametrize("universal_first", [True, False])
@pytest.mark.parametrize(
    ("kind", "signed", "source", "destination"),
    [("MAIN_OPTION", "50", SPOT, NAME), ("OPTION_MAIN", "-50", NAME, SPOT)],
)
async def test_bill_transfer_is_not_posted_twice(
    session, run, universal_first: bool, kind: str, signed: str, source: str, destination: str
) -> None:
    connection = await binance_connection(run)
    universal = build_row(
        UNIVERSAL, 0, asset="STB", amount="50", type=kind, status="CONFIRMED", timestamp=T
    )
    moved = _bill(0, TRANSFER, signed, createDate=T + 20_000)
    universal_key = f"universal_transfer:{universal['tranId']}"
    listed = ((UNIVERSAL, [universal]), (BILL, [moved]))

    for name, rows in listed if universal_first else listed[::-1]:
        await normalize_rows(session, connection.id, (name, rows))

    assert await keys(session, connection.id) == [universal_key]
    [row] = await typed(session, "binance_opt_bill", connection.id)
    assert (row.movement_key, row.transaction_id) == (universal_key, None)
    assert await holdings(session, connection.id) == {
        (source, "STB"): Decimal(-50),
        (destination, "STB"): Decimal(50),
    }
    await assert_balanced_and_quiet(session, connection.id)

    # The universal transfer no longer confirmed: the bill row posts the movement, its side the
    # row does not name against Unassigned.
    failed = {**universal, "status": "FAILED"}
    await normalize_rows(session, connection.id, (UNIVERSAL, [failed]))
    assert await keys(session, connection.id) == [_key(moved)]
    sign = Decimal(1) if Decimal(signed) > 0 else Decimal(-1)
    assert sorted(plain(await postings(session, _key(moved)))) == sorted(
        [(NAME, "STB", 50 * sign), ("Unassigned", "STB", -50 * sign)]
    )
    assert await _never_transfer(session, _key(moved)) is True
    [row] = await typed(session, "binance_opt_bill", connection.id)
    assert row.movement_key is None and row.transaction_id is not None
    await assert_balanced_and_quiet(session, connection.id)


async def test_trades_exercises_and_positions_are_typed_only(session, run) -> None:
    connection = await binance_connection(run)
    await normalize_rows(
        session,
        connection.id,
        ("opt-trades", [build_row("opt-trades", 0, symbol=SYMBOL)]),
        ("opt-exercises", [build_row("opt-exercises", 0, symbol=SYMBOL)]),
    )
    positions = _read("opt-positions", [build_row("opt-positions", symbol=SYMBOL)], T)
    await run(report_balances, connection, {"opt-positions": positions})

    assert await keys(session, connection.id) == []
    for table in ("binance_opt_trades", "binance_opt_exercises"):
        [row] = await typed(session, table, connection.id)
        assert row.transaction_id is None
    assert len(await typed(session, "binance_opt_positions", connection.id)) == 1
    # A contract is not an instrument: a position is no snapshot.
    assert await _snapshots(session, connection.id) == []
    await assert_balanced_and_quiet(session, connection.id)


async def test_old_bills_become_the_opening(session, run) -> None:
    connection = await binance_connection(run)
    old = [_bill(0, "EXERCISE_PNL", "3"), _bill(1, "FEE", "-1")]
    await normalize_rows(session, connection.id, (BILL, old))
    report = [_asset("STB", "10", "14")]
    await run(report_balances, connection, {ACCOUNT: _read(ACCOUNT, report, T + DAY_MS)})

    async with httpx.AsyncClient() as http:
        ctx = await context(session, connection.id, http)
        async with command_context(session, CONNECTOR, "binance.opening", {}) as cmd:
            made = await set_openings(await Run.start(ctx, cmd, ctx.connection))

    assert made == 1
    [opening] = [k for k in await keys(session, connection.id) if k.startswith("opening:options:")]
    # The records before the report explain 2 of its margin balance of 10: the opening is the
    # other 8. The positions' unrealised value (4) is in the report, and in no opening.
    assert plain(await postings(session, opening)) == [
        (NAME, "STB", Decimal(8)),
        ("Opening balances", "STB", Decimal(-8)),
    ]
    assert await check(session, connection.id, ()) == []


# The sync.


async def test_an_account_without_options_is_not_present(
    session, run, fake: FakeBinance, connector
) -> None:
    config = later_config()
    # The options histories not walked yet: their first walk is refused at its newest window.
    cursors = config[CURSORS_KEY]
    assert isinstance(cursors, dict)
    for label in (BILL, "opt-exercises"):
        del cursors[label]
    connection = await binance_connection(run, config=config)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert _FAMILY <= set(after.config[NOT_PRESENT_KEY])
    assert not [f for f in after.config.get("flags", []) if "opt" in f or "eapi" in f]
    asked = [s.label for s in fake.requests if ENDPOINTS[s.label].limit == "eapi"]
    # Its limits first, then each of the family once; no trade symbol is named.
    assert asked[0] == "opt-exchange-info"
    assert sorted(asked[1:]) == sorted(_FAMILY)
    [limit] = await typed(session, "binance_opt_exchange_info_rate_limits", after.id)
    assert (limit.rate_limit_type, limit.interval, limit.limit) == (
        "REQUEST_WEIGHT",
        "MINUTE",
        2400,
    )
    assert not [n for n, *_ in await _snapshots(session, after.id) if n == NAME]


async def test_the_options_wallet_reconciles_with_a_consistent_history(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.options = True
    fake.set_rows("spot-account", [build_row("spot-account", asset="STB", free="100", locked="0")])
    fake.set_rows(ACCOUNT, [_asset("STB", "100", "105")])
    connection = await binance_connection(run, config=later_config())
    first = await run_binance_sync(session, connection.id, fake.handler)
    assert first.status == "healthy", first.last_error

    start = clock.ms

    def hours(n: int) -> int:
        return start + n * HOUR_MS

    # 50 into Options, listed by the universal transfer history and the bill (rule 2); a
    # premium paid, its fee, an exercise's profit and its fee; the positions' unrealised value
    # moves from 5 to -2.25.
    fake.add(
        UNIVERSAL,
        build_row(
            UNIVERSAL,
            0,
            asset="STB",
            amount="50",
            type="MAIN_OPTION",
            status="CONFIRMED",
            timestamp=hours(1),
        ),
        when={"type": "MAIN_OPTION"},
    )
    fake.add(
        BILL,
        _bill(0, TRANSFER, "50", createDate=hours(1) + 2_000),
        _bill(1, "CONTRACT", "-10", createDate=hours(2)),
        _bill(2, "FEE", "-0.5", createDate=hours(2)),
        _bill(3, "EXERCISE_PNL", "3", createDate=hours(3)),
        _bill(4, "EXERCISE_FEE", "-0.25", createDate=hours(3)),
    )
    fake.set_rows("spot-account", [build_row("spot-account", asset="STB", free="50", locked="0")])
    fake.set_rows(ACCOUNT, [_asset("STB", "142.25", "140")])
    notifier = RecordingNotifier()
    clock.ms += 6 * HOUR_MS
    after = await run_binance_sync(session, connection.id, fake.handler, notifier=notifier)

    assert after.status == "healthy", after.last_error
    assert not [w for w in after.warnings if _CHANGED in w]
    assert not [m for m in notifier.messages if _CHANGED in m]
    live = await keys(session, after.id)
    assert [k for k in live if k.startswith("universal_transfer:")] == [
        f"universal_transfer:{build_row(UNIVERSAL, 0)['tranId']}"
    ]
    assert len([k for k in live if k.startswith("opt_bill:")]) == 4
    reported = [q for name, _, q, _ in await _snapshots(session, after.id) if name == NAME]
    assert reported == [Decimal(105), Decimal(140)]
    # The premium's symbol is walked for its trades.
    assert {s.params["symbol"] for s in fake.calls("opt-trades")} == {SYMBOL}


async def test_options_trades_are_walked_for_the_symbols_the_bill_names(
    session, run, fake: FakeBinance, connector, clock: Clock
) -> None:
    fake.options = True
    traded = clock.ms - 2 * DAY_MS
    fake.add(
        BILL,
        _bill(0, "CONTRACT", "-1", createDate=traded),
        _bill(1, TRANSFER, "5", createDate=traded),
    )
    fake.add("opt-trades", build_row("opt-trades", 0, symbol=SYMBOL, time=traded))
    connection = await binance_connection(run, config=later_config())
    # A bill of a symbol more than 3 months old: Binance serves none of its trades now.
    old = _bill(7, "FEE", "-0.1", symbol="OLD-200101-1-P", createDate=clock.ms - 100 * DAY_MS)
    await ingest(session, connection.id, BILL, [old])

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    calls = fake.calls("opt-trades")
    assert {s.params["symbol"] for s in calls} == {SYMBOL}
    assert "opt-trades:OLD-200101-1-P" not in after.config[CURSORS_KEY]
    for seen in calls:
        # By time, from no further back than 3 months; never by trade id.
        assert "fromId" not in seen.params
        assert int(seen.params["startTime"]) >= clock.ms - 90 * DAY_MS
    [trade] = await typed(session, "binance_opt_trades", after.id)
    assert trade.symbol == SYMBOL and trade.transaction_id is None
    assert after.config[CURSORS_KEY][f"opt-trades:{SYMBOL}"]["complete"] is True
    for seen in fake.calls(BILL):
        assert seen.params["currency"] == "USDT" and "recordId" not in seen.params


@pytest.mark.parametrize("failure", ["shape", "error"])
async def test_the_options_limits_read_that_fails_never_stops_the_sync(
    session, run, fake: FakeBinance, connector, clock: Clock, failure: str
) -> None:
    label = "opt-exchange-info"
    if failure == "shape":
        fake.set_envelope(label, timezone=[[]])
    else:
        fake.fail(label, status=500)
    connection = await binance_connection(run, config=later_config())

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert [f for f in after.config["flags"] if label in f] == [limits_unread_flag(label)]
    assert await typed(session, "binance_opt_exchange_info", after.id) == []
    # The host keeps the assumed limit; the futures hosts take their own.
    flags: list[str] = []
    async with fake.http() as http:
        client = client_for(clock, http)
        await client.sync_clock()
        await read_limits(await context(session, after.id, http), client, flag=flags.append)
    assert flags == [limits_unread_flag(label)]
    assert (client.caps["eapi"], client.caps["fapi"], client.caps["dapi"]) == (600, 2400, 2400)


# An Ed25519 key off `api`.


def _refuse_host(fake: FakeBinance, limit: str, status: int, code: int | None) -> None:
    for endpoint in ENDPOINTS.values():
        if endpoint.limit == limit and endpoint.signed:
            fake.fail(endpoint.label, status=status, code=code)


def _host_labels(limit: str) -> set[str]:
    built = {*PHASE_B_BUILT, *SYMBOL_TRADES, *LIMITS_INFO}
    return {label for label in built if ENDPOINTS[label].limit == limit}


# Ruling R22: after `api` accepted the key, a derivatives host's 401, -1022, -2015 or -2014.
_REFUSALS = [
    ("fapi", 401, None),
    ("eapi", 400, -1022),
    ("eapi", 401, -2015),
    ("fapi", 400, -2015),
]
_KEY_TYPES = ["ed25519", "hmac"]


def _symbol_named(fake: FakeBinance, limit: str, at_ms: int) -> str:
    """A history row of the host that names a symbol, so its trades are asked."""
    if limit == "fapi":
        fake.add("um-income", build_row("um-income", 0, symbol="AAASTB", time=at_ms))
        return "um-trades"
    fake.add(BILL, _bill(0, "CONTRACT", "-1", createDate=at_ms))
    return "opt-trades"


def _asked(fake: FakeBinance, limit: str) -> list[str]:
    return [s.label for s in fake.requests if ENDPOINTS[s.label].limit == limit]


async def _forget_flags(run, connection_id) -> None:
    """As if no flag had been published yet: what a sync raises shows again."""

    async def forget(ctx) -> None:
        await update_config(ctx, connection_id, remove=["flags"])

    await run(forget)


@pytest.mark.parametrize("key_type", _KEY_TYPES)
@pytest.mark.parametrize(("limit", "status", "code"), _REFUSALS)
async def test_a_key_refused_on_another_host_is_a_flag_not_a_rejection(
    session,
    run,
    connector,
    clock: Clock,
    key_type: str,
    limit: str,
    status: int,
    code: int | None,
) -> None:
    fake = FakeBinance(now=clock, options=True)
    _refuse_host(fake, limit, status, code)
    host = f"{limit}.binance.com"
    told = host_refused_flag(host, "ed25519" if key_type == "ed25519" else "hmac")
    connection = await binance_connection(run, config=later_config(), key_type=key_type)
    began = clock.ms

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    assert [f for f in after.config["flags"] if host in f] == [told]
    assert not [f for f in after.config["flags"] if "could not be read" in f]
    [(refused_host, since)] = after.config[REFUSED_HOSTS_KEY].items()
    assert refused_host == limit and began <= since <= clock.ms
    # A host's refusal of the key is not the product's absence.
    assert not _host_labels(limit) & set(after.config.get(NOT_PRESENT_KEY, {}))
    on_host = _asked(fake, limit)
    # Its limits (unsigned), then the first signed request, refused; nothing more of it.
    assert len(on_host) == 2 and ENDPOINTS[on_host[1]].signed
    # The other derivatives hosts are asked as usual.
    for other in {"fapi", "dapi", "eapi"} - {limit}:
        assert other not in str(after.config["flags"])
        assert _asked(fake, other)

    # The same key: not asked for 7 days; then asked again, refused again and told once.
    await _forget_flags(run, connection.id)
    for days, asked in ((1, 0), (7, 2)):
        fake.requests.clear()
        clock.ms += days * DAY_MS
        began = clock.ms
        after = await run_binance_sync(session, connection.id, fake.handler)
        assert after.status == "healthy", after.last_error
        assert len(_asked(fake, limit)) == asked
    assert after.config["flags"].count(told) == 1
    assert began <= after.config[REFUSED_HOSTS_KEY][limit] <= clock.ms


@pytest.mark.parametrize("key_type", _KEY_TYPES)
@pytest.mark.parametrize(("status", "code"), [(401, None), (400, -2015), (400, -1022)])
async def test_the_gate_refused_on_api_is_still_a_rejected_key(
    session, run, connector, clock: Clock, key_type: str, status: int, code: int | None
) -> None:
    fake = FakeBinance(now=clock, options=True)
    fake.fail("api-restrictions", status=status, code=code)
    connection = await binance_connection(run, config=later_config(), key_type=key_type)

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "needs_reauth"
    assert after.config.get(REFUSED_HOSTS_KEY, {}) == {}
    assert [seen.label for seen in fake.requests] == ["time", "api-restrictions"]


@pytest.mark.parametrize(("limit", "status", "code"), _REFUSALS)
async def test_a_new_hmac_key_asks_a_refused_host_again_at_once(
    session, run, connector, clock: Clock, limit: str, status: int, code: int | None
) -> None:
    fake = FakeBinance(now=clock, options=True)
    _refuse_host(fake, limit, status, code)
    trades = _symbol_named(fake, limit, clock.ms - DAY_MS)
    host = f"{limit}.binance.com"
    connection = await binance_connection(run, config=later_config(), key_type="ed25519")
    refused = await run_binance_sync(session, connection.id, fake.handler)
    assert set(refused.config[REFUSED_HOSTS_KEY]) == {limit}

    # The owner switches to an HMAC key, as the flag says; a key update clears the refusals
    # (as the route does).
    async def switch(ctx) -> None:
        await store_credential(ctx, KEY, connection.id, "api_secret", API_SECRET, None)
        await update_config(ctx, connection.id, {"key_type": "hmac"}, remove=[REFUSED_HOSTS_KEY])

    await run(switch)
    await _forget_flags(run, connection.id)
    fake.clear_failures()
    fake.requests.clear()
    clock.ms += HOUR_MS * 5

    after = await run_binance_sync(session, connection.id, fake.handler)

    assert after.status == "healthy", after.last_error
    # The limits read, the balances, the histories and the trades, at once.
    prefix = "um" if limit == "fapi" else "opt"
    history = "um-income" if limit == "fapi" else BILL
    wanted = {
        f"{prefix}-exchange-info",
        f"{prefix}-account",
        f"{prefix}-positions",
        history,
        trades,
    }
    assert wanted <= set(_asked(fake, limit))
    assert key_type_flag(host) not in after.config.get("flags", [])
    assert after.config.get(REFUSED_HOSTS_KEY, {}) == {}


@pytest.mark.parametrize("label", ["um-exchange-info", "opt-exchange-info"])
async def test_an_unsigned_limits_read_refused_says_nothing_of_the_key(
    session, run, connector, clock: Clock, label: str
) -> None:
    fake = FakeBinance(now=clock, options=True)
    fake.fail(label, status=401)
    limit = ENDPOINTS[label].limit
    host = ENDPOINTS[label].host
    connection = await binance_connection(run, config=later_config(), key_type="ed25519")

    after = await run_binance_sync(session, connection.id, fake.handler)

    # The read carried no key: the generic limits flag, and the host is asked as usual.
    assert after.status == "healthy", after.last_error
    assert limits_unread_flag(label) in after.config["flags"]
    assert key_type_flag(host) not in after.config["flags"]
    assert after.config.get(REFUSED_HOSTS_KEY, {}) == {}
    assert len([s for s in _asked(fake, limit) if ENDPOINTS[s].signed]) > 1
