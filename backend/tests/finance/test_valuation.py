from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from financial_gate.core.settings_store import set_setting
from financial_gate.modules.finance.accounts import close_account
from financial_gate.modules.finance.instruments import create_instrument, get_currency, set_hidden
from financial_gate.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from financial_gate.modules.finance.pricing import record_fx_rate, record_price
from financial_gate.modules.finance.snapshots import record_snapshot
from financial_gate.modules.finance.valuation import balances_asof, holdings_asof, net_worth_series

LON = ZoneInfo("Europe/London")


def at(y: int, m: int, d: int, hh: int = 12) -> datetime:
    return datetime(y, m, d, hh, tzinfo=LON)


async def snap(run, account, instrument, qty: str, when: datetime) -> None:
    await run(
        record_snapshot,
        account_id=account.id,
        instrument_id=instrument.id,
        quantity=Decimal(qty),
        as_of=when,
    )


async def test_snapshot_mode_uses_latest_on_or_before_day(session, run, make_account, gbp) -> None:
    acc = await make_account()
    await snap(run, acc, gbp, "1000", at(2026, 11, 1))
    await snap(run, acc, gbp, "1500", at(2026, 11, 5))
    assert [r.quantity for r in await balances_asof(session, date(2026, 11, 4))] == [
        Decimal("1000")
    ]
    assert [r.quantity for r in await balances_asof(session, date(2026, 11, 5))] == [
        Decimal("1500")
    ]


async def test_same_day_snapshots_latest_time_wins(session, run, make_account, gbp) -> None:
    acc = await make_account()
    await snap(run, acc, gbp, "1500", at(2026, 11, 5, 9))
    await snap(run, acc, gbp, "1700", at(2026, 11, 5, 18))
    assert [r.quantity for r in await balances_asof(session, date(2026, 11, 5))] == [
        Decimal("1700")
    ]


async def test_transactions_mode_excludes_void(session, run, make_account, gbp) -> None:
    acc = await make_account(balance_mode="transactions")
    other = await make_account(name="Other", ledger_type="equity", subtype="other")
    first = TransactionInput(
        occurred_at=at(2026, 11, 1),
        description="in",
        postings=[
            PostingInput(acc.id, gbp.id, Decimal("100")),
            PostingInput(other.id, gbp.id, Decimal("-100")),
        ],
    )
    second = TransactionInput(
        occurred_at=at(2026, 11, 3),
        description="in",
        postings=[
            PostingInput(acc.id, gbp.id, Decimal("50")),
            PostingInput(other.id, gbp.id, Decimal("-50")),
        ],
    )
    await run(post_transaction, first)
    tx2 = await run(post_transaction, second)
    await run(void_transaction, tx2.id)
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 4))}
    assert rows[acc.id] == Decimal("100")


async def test_hybrid_adds_postings_after_snapshot(session, run, make_account, gbp) -> None:
    manual_bank = await make_account(name="Manual bank", balance_mode="hybrid")
    monzo = await make_account(name="Monzo", balance_mode="transactions")
    await snap(run, manual_bank, gbp, "2000", at(2026, 11, 1, 18))
    for when, qty in ((at(2026, 11, 1, 9), "100"), (at(2026, 11, 5, 10), "500")):
        await run(
            post_transaction,
            TransactionInput(
                occurred_at=when,
                description="t",
                postings=[
                    PostingInput(monzo.id, gbp.id, Decimal(f"-{qty}")),
                    PostingInput(manual_bank.id, gbp.id, Decimal(qty)),
                ],
            ),
        )
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 5))}
    assert rows[manual_bank.id] == Decimal("2500")
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 4))}
    assert rows[manual_bank.id] == Decimal("2000")


async def test_fiat_valued_direct_inverse_and_via_eur(
    session, run, make_account, gbp, usd, eur
) -> None:
    cny = await get_currency(session, "CNY")
    usd_acc, eur_acc, cny_acc = [await make_account(name=n) for n in ("USD", "EUR", "CNY")]
    day = date(2026, 11, 10)
    await snap(run, usd_acc, usd, "100", at(2026, 11, 10))
    await snap(run, eur_acc, eur, "100", at(2026, 11, 10))
    await snap(run, cny_acc, cny, "1000", at(2026, 11, 10))
    await run(
        record_fx_rate,
        base_instrument_id=usd.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.8"),
        as_of_date=day,
    )
    await run(
        record_fx_rate,
        base_instrument_id=gbp.id,
        quote_instrument_id=eur.id,
        rate=Decimal("1.25"),
        as_of_date=day,
    )
    await run(
        record_fx_rate,
        base_instrument_id=eur.id,
        quote_instrument_id=cny.id,
        rate=Decimal("8"),
        as_of_date=day,
    )
    values = {h.symbol: h.value for h in await holdings_asof(session, day)}
    assert values["USD"] == Decimal("80")
    assert values["EUR"] == Decimal("80")
    assert values["CNY"] == Decimal("100")


async def test_missing_fx_is_flagged_not_zero(session, run, make_account, gbp) -> None:
    ngn = await run(
        create_instrument,
        kind="fiat",
        symbol="NGN",
        name="Naira",
        decimals=2,
        quote_instrument_id=None,
        identifiers=[],
    )
    bank, fx_wallet = (
        await make_account(name="Bank"),
        await make_account(name="Multi-currency wallet"),
    )
    await snap(run, bank, gbp, "1000", at(2026, 11, 10))
    await snap(run, fx_wallet, ngn, "50000", at(2026, 11, 10))
    holdings = {h.symbol: h for h in await holdings_asof(session, date(2026, 11, 10))}
    assert (holdings["NGN"].status, holdings["NGN"].value) == ("missing_fx", None)
    [point] = await net_worth_series(session, date(2026, 11, 10), date(2026, 11, 10))
    assert (point.value, point.missing_count) == (Decimal("1000"), 1)


async def test_crypto_valued_with_price_then_fx(session, run, make_account, btc, usd, gbp) -> None:
    wallet = await make_account(name="Wallet", subtype="wallet")
    day = date(2026, 11, 10)
    await snap(run, wallet, btc, "0.5", at(2026, 11, 10))
    [h] = await holdings_asof(session, day)
    assert h.status == "missing_price"
    await run(
        record_price,
        instrument_id=btc.id,
        quote_instrument_id=usd.id,
        price=Decimal("60000"),
        as_of_date=day,
    )
    await run(
        record_fx_rate,
        base_instrument_id=usd.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.8"),
        as_of_date=day,
    )
    [h] = await holdings_asof(session, day)
    assert (h.status, h.value) == ("ok", Decimal("24000"))


async def test_hidden_and_closed_and_non_balance_accounts_excluded(
    session, run, make_account, gbp, usd
) -> None:
    spam = await run(
        create_instrument,
        kind="crypto",
        symbol="SPAM",
        name="Airdrop",
        decimals=18,
        quote_instrument_id=usd.id,
        identifiers=[],
    )
    wallet = await make_account(name="Wallet", subtype="wallet")
    old = await make_account(name="Old bank")
    await snap(run, wallet, spam, "1000000", at(2026, 11, 1))
    await snap(run, old, gbp, "10", at(2026, 11, 1))
    await run(set_hidden, spam.id, True)
    await run(close_account, old.id, date(2026, 11, 5))
    assert [h.symbol for h in await holdings_asof(session, date(2026, 11, 4))] == ["GBP"]
    assert await holdings_asof(session, date(2026, 11, 5)) == []


async def test_liability_reduces_net_worth(session, run, make_account, gbp) -> None:
    bank = await make_account(name="Bank")
    card = await make_account(name="Card", ledger_type="liability", subtype="other")
    await snap(run, bank, gbp, "2000", at(2026, 11, 10))
    await snap(run, card, gbp, "-500", at(2026, 11, 10))
    [point] = await net_worth_series(session, date(2026, 11, 10), date(2026, 11, 10))
    assert point.value == Decimal("1500")


async def test_reporting_currency_change_revalues(session, run, make_account, gbp, usd) -> None:
    bank = await make_account(name="Bank")
    await snap(run, bank, gbp, "100", at(2026, 11, 10))
    await run(
        record_fx_rate,
        base_instrument_id=usd.id,
        quote_instrument_id=gbp.id,
        rate=Decimal("0.8"),
        as_of_date=date(2026, 11, 10),
    )
    await run(set_setting, "finance.reporting_currency", "USD")
    [h] = await holdings_asof(session, date(2026, 11, 10))
    assert h.value == Decimal("125")


async def test_series_has_one_row_per_day(session) -> None:
    days = [p.day for p in await net_worth_series(session, date(2026, 11, 1), date(2026, 11, 3))]
    assert days == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]


async def fx(session, base, quote, day: date) -> Decimal | None:
    return await session.scalar(
        text("select finance.v1_fx_rate(:base, :quote, :day)"),
        {"base": base.id, "quote": quote.id, "day": day},
    )


async def rate(run, base, quote, value: str, day: date, source: str = "manual") -> None:
    await run(
        record_fx_rate,
        base_instrument_id=base.id,
        quote_instrument_id=quote.id,
        rate=Decimal(value),
        as_of_date=day,
        source=source,
    )


async def test_same_currency_rate_is_exactly_one(session, gbp) -> None:
    assert str(await fx(session, gbp, gbp, date(2026, 11, 10))) == "1"


async def test_fresher_via_eur_path_beats_older_direct_rate(session, run, usd, eur, gbp) -> None:
    await rate(run, usd, gbp, "0.8", date(2026, 11, 1))
    await rate(run, usd, eur, "0.9", date(2026, 11, 9))
    await rate(run, eur, gbp, "0.85", date(2026, 11, 8))
    assert await fx(session, usd, gbp, date(2026, 11, 10)) == Decimal("0.765")
    # Before the second leg exists there is no via-EUR path, so the direct rate stands.
    assert await fx(session, usd, gbp, date(2026, 11, 7)) == Decimal("0.8")


async def test_via_eur_path_is_as_old_as_its_older_leg(session, run, usd, eur, gbp) -> None:
    await rate(run, usd, gbp, "0.8", date(2026, 11, 1))
    await rate(run, usd, eur, "0.9", date(2026, 11, 9))
    await rate(run, eur, gbp, "0.85", date(2026, 10, 30))
    assert await fx(session, usd, gbp, date(2026, 11, 10)) == Decimal("0.8")


async def test_same_day_direct_beats_via_eur(session, run, usd, eur, gbp) -> None:
    day = date(2026, 11, 10)
    await rate(run, usd, gbp, "0.8", day)
    await rate(run, usd, eur, "0.9", day)
    await rate(run, eur, gbp, "0.85", day)
    assert await fx(session, usd, gbp, day) == Decimal("0.8")


async def test_fresher_inverse_beats_older_direct_and_loses_a_tie(session, run, usd, gbp) -> None:
    await rate(run, usd, gbp, "0.8", date(2026, 11, 1))
    await rate(run, gbp, usd, "1.6", date(2026, 11, 5))
    assert await fx(session, usd, gbp, date(2026, 11, 10)) == Decimal("0.625")
    await rate(run, usd, gbp, "0.7", date(2026, 11, 5))
    assert await fx(session, usd, gbp, date(2026, 11, 10)) == Decimal("0.7")


async def test_manual_fx_rate_preferred_within_a_day(session, run, usd, gbp) -> None:
    day = date(2026, 11, 10)
    await rate(run, usd, gbp, "0.8", day)
    await rate(run, usd, gbp, "0.7", day, source="ecb")
    # Recorded later, so only the manual preference keeps it from winning.
    await session.execute(
        text(
            "update finance.fx_rates set recorded_at = now() + interval '1 hour'"
            " where source = 'ecb'"
        )
    )
    assert await fx(session, usd, gbp, day) == Decimal("0.8")


async def price(run, instrument, quote, value: str, day: date, source: str = "manual") -> None:
    await run(
        record_price,
        instrument_id=instrument.id,
        quote_instrument_id=quote.id,
        price=Decimal(value),
        as_of_date=day,
        source=source,
    )


async def test_manual_price_preferred_within_a_day(session, run, make_account, btc, gbp) -> None:
    wallet = await make_account(name="Wallet", subtype="wallet")
    day = date(2026, 11, 10)
    await snap(run, wallet, btc, "1", at(2026, 11, 10))
    await price(run, btc, gbp, "50000", day)
    await price(run, btc, gbp, "40000", day, source="coingecko")
    await session.execute(
        text(
            "update finance.prices set recorded_at = now() + interval '1 hour'"
            " where source = 'coingecko'"
        )
    )
    [h] = await holdings_asof(session, day)
    assert (h.unit_price, h.value) == (Decimal("50000"), Decimal("50000"))


async def test_latest_price_on_or_before_the_day(session, run, make_account, btc, gbp) -> None:
    wallet = await make_account(name="Wallet", subtype="wallet")
    await snap(run, wallet, btc, "2", at(2026, 11, 1))
    await price(run, btc, gbp, "100", date(2026, 11, 1))
    await price(run, btc, gbp, "200", date(2026, 11, 5))
    await price(run, btc, gbp, "300", date(2026, 11, 20))
    values = [(await holdings_asof(session, date(2026, 11, d)))[0].value for d in (4, 5, 10)]
    assert values == [Decimal("200"), Decimal("400"), Decimal("400")]


async def test_hybrid_ignores_postings_booked_after_the_day(
    session, run, make_account, gbp
) -> None:
    bank = await make_account(name="Bank", balance_mode="hybrid")
    other = await make_account(name="Other", balance_mode="transactions")
    await snap(run, bank, gbp, "2000", at(2026, 11, 1, 18))
    # Happened after the snapshot, but only booked on the 6th.
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=at(2026, 11, 3),
            booking_date=date(2026, 11, 6),
            description="t",
            postings=[
                PostingInput(other.id, gbp.id, Decimal("-300")),
                PostingInput(bank.id, gbp.id, Decimal("300")),
            ],
        ),
    )
    for day, expected in ((date(2026, 11, 5), "2000"), (date(2026, 11, 6), "2300")):
        rows = {r.account_id: r.quantity for r in await balances_asof(session, day)}
        assert rows[bank.id] == Decimal(expected)


async def test_balance_rows_report_the_snapshot_used(session, run, make_account, gbp) -> None:
    bank = await make_account(name="Bank", balance_mode="hybrid")
    other = await make_account(name="Other", balance_mode="transactions")
    await snap(run, bank, gbp, "2000", at(2026, 11, 1, 18))
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=at(2026, 11, 3),
            description="t",
            postings=[
                PostingInput(other.id, gbp.id, Decimal("-300")),
                PostingInput(bank.id, gbp.id, Decimal("300")),
            ],
        ),
    )
    rows = {r.account_id: r for r in await balances_asof(session, date(2026, 11, 5))}
    assert (
        rows[bank.id].quantity,
        rows[bank.id].snapshot_quantity,
        rows[bank.id].snapshot_date,
    ) == (Decimal("2300"), Decimal("2000"), date(2026, 11, 1))
    assert (rows[other.id].snapshot_quantity, rows[other.id].snapshot_date) == (None, None)
