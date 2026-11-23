"""The eToro normalizer, statement side (design §5.6-5.7, §5.12): the statement's cash events,
its enrichment of what the API posted, and which source posts an event both have.

Synthetic data only: the statement rows are shaped like the owner's (statement-shapes.txt) with
invented amounts, dates, position ids and ISINs.
"""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance.connector_run import holdings
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.fields import ACCOUNT_ACTIVITY, ACCOUNT_SUMMARY
from lararium.modules.finance.etoro.merge import TradeEvent, disagreements, posting_values
from lararium.modules.finance.etoro.normalize import NormalizeReport
from lararium.modules.finance.etoro.statement import parse_statement
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    get_currency,
)
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.source_reviews import list_reviews, resolve_review

from .fixtures import (
    CASH_ACCOUNT,
    INSTRUMENT_ID,
    SYMBOL,
    aggregate_body,
    balances_body,
    instrument_meta,
    lookup_body,
    me_json,
    trade_row,
)
from .statement_fixtures import (
    ISIN,
    POSITION_1,
    activity_row,
    build_statement,
    build_workbook,
    closed_row,
    dividend_row,
    statement_sheets,
    summary,
)
from .test_normalize_api import Ledger, _connection, _delete_derived, _postings, _rows

POSITION = int(POSITION_1)
ORDER = POSITION + 500
OPENED = "02/10/2026 14:30:05"
CLOSED = "20/10/2026 15:01:44"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
TRADING = "eToro Trading"
USERNAME = me_json()["username"]
BASE = [
    ("me", me_json()),
    ("balances", balances_body()),
    ("aggregate", aggregate_body([(INSTRUMENT_ID, "USD", "0")])),
    ("instruments", instrument_meta()),
]


def api_trade(**overrides: Any) -> dict[str, Any]:
    return (
        trade_row(POSITION)
        | {
            "openTimestamp": "2026-10-02T14:30:05.120Z",
            "closeTimestamp": "2026-10-20T15:01:44.500Z",
            "investment": Decimal("500.00"),
            "initialInvestment": Decimal("500.00"),
            "netProfit": Decimal("20.00"),
            "units": Decimal("4"),
            "fees": Decimal("0"),
            "orderId": ORDER,
        }
        | overrides
    )


TRADES = [("trade_history", api_trade()), ("order_lookup", lookup_body(ORDER, POSITION))]


def transfer_in(at: str = "01/10/2026 10:00:00", amount: str = "1260.00") -> dict[str, str]:
    return activity_row(
        at, "Transfer: GBP > USD", amount, details="GBP", equity=amount, balance=amount
    )


def conversion_fee(at: str, amount: str = "-6.30", kind: str = "Deposit") -> dict[str, str]:
    return activity_row(at, f"{kind} Conversion Fee", amount, change="0")


def open_row(units: str = "4", balance: str = "760.00") -> dict[str, str]:
    return activity_row(
        OPENED,
        "Open Position",
        "500",
        details=f"{SYMBOL}/USD",
        units=units,
        position_id=POSITION_1,
        change="0",
        balance=balance,
        asset_type="Stocks",
    )


def close_row(balance: str = "1280.00") -> dict[str, str]:
    return activity_row(
        CLOSED,
        "Position closed",
        "520.00",
        details=f"{SYMBOL}/USD",
        units="4",
        position_id=POSITION_1,
        change="20.00",
        equity="1280.00",
        balance=balance,
        asset_type="Stocks",
    )


def trade_statement(closed_units: str = "4") -> bytes:
    """A statement of one transfer in (with its fee) and one position opened and closed;
    `closed_units` the units Closed Positions says the position opened and closed with."""
    return build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            conversion_fee("01/10/2026 10:00:00"),
            open_row(),
            close_row(),
        ],
        [closed_row() | {"Units / Contracts": closed_units}],
        financial=[
            {
                "Name": "Dividends",
                "01/10/2026 - 31/10/2026": "0.00",
                "Year to date": "0.00",
                "Total": "0.00",
            }
        ],
    )


class StatementLedger(Ledger):
    """A Ledger that also uploads statements, as the API's upload route does."""

    def __init__(self, session, connection, root) -> None:
        super().__init__(session, connection)
        self.documents = LocalDocumentStore(root)
        self.reports: list[NormalizeReport] = []

    async def upload(self, body: bytes) -> NormalizeReport:
        ctx = dataclasses.replace(await self.context(), documents=self.documents)
        await etoro_sync.ingest_statement(ctx, body, "statement.xlsx")
        return self.reports[-1]

    def disagreement_messages(self) -> list[str]:
        return [m for m in self.notifier.messages if "disagree" in m]


@pytest.fixture
async def ledger(session, run, tmp_path, monkeypatch) -> StatementLedger:
    found = StatementLedger(session, await _connection(run), tmp_path)
    normalize = etoro_sync.normalize

    async def spy(ctx, raw_record_ids):
        report = await normalize(ctx, raw_record_ids)
        found.reports.append(report)
        return report

    monkeypatch.setattr(etoro_sync, "normalize", spy)
    return found


async def _ledger(session, ledger: Ledger) -> dict[str, tuple]:
    """The connection's live transactions by external id: (occurred_at, booking date,
    description, counterparty identifier, postings), postings as (account, symbol, quantity,
    cost, price, is_fee) with exact numbers, no UUIDs."""
    rows = await _rows(
        session,
        "select t.id, t.external_id, t.occurred_at, t.booking_date, t.description,"
        " ci.value as counterparty, t.bank_transfer from finance.transactions t"
        " left join finance.account_identifiers ci on ci.id = t.counterparty_identifier_id"
        " where t.connection_id = :c and t.status <> 'void'",
        c=ledger.connection.id,
    )
    found: dict[str, tuple] = {}
    for row in rows:
        postings = await _rows(
            session,
            "select a.name, i.symbol, p.quantity, p.cost_per_unit, p.price_per_unit, p.is_fee"
            " from finance.postings p join finance.accounts a on a.id = p.account_id"
            " join finance.instruments i on i.id = p.instrument_id"
            " where p.transaction_id = :t order by p.position",
            t=row.id,
        )
        found[row.external_id] = (
            row.occurred_at,
            row.booking_date,
            row.description,
            row.counterparty,
            row.bank_transfer,
            [tuple(p) for p in postings],
        )
    return found


def _key(body: bytes, type_: str) -> str:
    """The statement row's key (Decision 10), as the reader gives it."""
    statement = parse_statement(body, etoro_sync.STATEMENT_ZONE)
    [key] = [
        r.key for r in statement.rows if r.sheet == ACCOUNT_ACTIVITY and r.cells["Type"] == type_
    ]
    return key


def _expected_trade_ledger(body: bytes) -> dict[str, tuple]:
    """What the API's trade and the statement's transfer post, whichever arrived first."""
    return {
        f"activity:{_key(body, 'Transfer: GBP > USD')}": (
            datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
            date(2026, 10, 1),
            "Transfer from eToro Money",
            CASH_ACCOUNT,
            False,
            [
                (TRADING, "USD", Decimal("1260.00"), None, None, False),
                ("In transit", "USD", Decimal("-1260.00"), None, None, False),
            ],
        ),
        f"open:{POSITION}": (
            datetime(2026, 10, 2, 14, 30, 5, 120000, tzinfo=UTC),
            date(2026, 10, 2),
            f"Buy 4 {SYMBOL}",
            None,
            False,
            [
                (TRADING, SYMBOL, Decimal(4), Decimal(125), None, False),
                (TRADING, "USD", Decimal(-500), None, None, False),
            ],
        ),
        f"close:{POSITION}": (
            datetime(2026, 10, 20, 15, 1, 44, 500000, tzinfo=UTC),
            date(2026, 10, 20),
            f"Sell 4 {SYMBOL}",
            None,
            False,
            [
                (TRADING, SYMBOL, Decimal(-4), None, Decimal(130), False),
                (TRADING, "USD", Decimal(520), None, None, False),
            ],
        ),
    }


async def _leg_fees(session) -> list[tuple]:
    rows = await _rows(
        session,
        "select t.external_id, i.symbol, f.amount from finance.transfer_leg_fees f"
        " join finance.transactions t on t.id = f.transaction_id"
        " join finance.instruments i on i.id = f.instrument_id order by 1",
    )
    return [tuple(r) for r in rows]


# The merge rule, pure (design §5.7) -----------------------------------------------------------


def _event(**overrides: Any) -> TradeEvent:
    values: dict[str, Any] = {
        "units": Decimal(4),
        "cash": Decimal("500.00"),
        "profit": None,
        "booking_date": date(2026, 10, 2),
        "occurred_at": datetime(2026, 10, 2, 14, 30, 5, tzinfo=UTC),
    } | overrides
    return TradeEvent(**values)


def test_posting_values_and_disagreements() -> None:
    api = _event(cash=Decimal("500.0"))
    same = _event(occurred_at=datetime(2026, 10, 2, 14, 30, 6, tzinfo=UTC))
    # Equal numbers in another scale, and another second of the same day, agree.
    assert disagreements(api, same) == []
    statement = _event(
        units=Decimal(5),
        profit=Decimal("1.5"),
        booking_date=date(2026, 10, 3),
        occurred_at=datetime(2026, 10, 3, 0, 30, tzinfo=UTC),
    )
    # A profit only one source states is not a disagreement.
    assert disagreements(api, statement) == [
        ("units", "4", "5"),
        ("booking_date", "2026-10-02", "2026-10-03"),
    ]
    assert posting_values(api, statement, {}) == api
    assert posting_values(None, statement, {}) == statement
    assert posting_values(api, None, {"units": "statement"}) == api
    taken = posting_values(api, statement, {"units": "statement", "booking_date": "statement"})
    assert taken == dataclasses.replace(
        api, units=Decimal(5), booking_date=date(2026, 10, 3), occurred_at=statement.occurred_at
    )
    with pytest.raises(ValueError):
        posting_values(None, None, {})


# Cash events (design §5.7) ----------------------------------------------------------------------


async def test_transfer_rows_are_legs(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            activity_row(
                "03/10/2026 09:00:00",
                "Transfer: USD > GBP",
                "-1000.00",
                details="GBP",
                balance="260.00",
            ),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    assert found[f"activity:{_key(body, 'Transfer: GBP > USD')}"][2:] == (
        "Transfer from eToro Money",
        CASH_ACCOUNT,
        False,
        [
            (TRADING, "USD", Decimal("1260.00"), None, None, False),
            ("In transit", "USD", Decimal("-1260.00"), None, None, False),
        ],
    )
    assert found[f"activity:{_key(body, 'Transfer: USD > GBP')}"][2:] == (
        "Transfer to eToro Money",
        CASH_ACCOUNT,
        False,
        [
            (TRADING, "USD", Decimal("-1000.00"), None, None, False),
            ("In transit", "USD", Decimal("1000.00"), None, None, False),
        ],
    )
    # Typed, each linked to its transaction.
    rows = await _rows(
        session,
        "select a.type, a.amount, a.realized_equity_change, a.date, a.transaction_id"
        " from finance.etoro_activity a where a.connection_id = :c order by a.date",
        c=ledger.connection.id,
    )
    assert [tuple(r)[:4] for r in rows] == [
        (
            "Transfer: GBP > USD",
            Decimal("1260.00"),
            Decimal("1260.00"),
            datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        ),
        (
            "Transfer: USD > GBP",
            Decimal("-1000.00"),
            Decimal("-1000.00"),
            datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
        ),
    ]
    assert all(r.transaction_id is not None for r in rows)
    assert await find_unbalanced_transactions(session) == []


async def test_conversion_fee_row_no_postings_and_leg_fee(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            conversion_fee("01/10/2026 10:00:00"),
            # The fee of a transfer out, the row right after it (a second later).
            activity_row(
                "03/10/2026 09:00:00",
                "Transfer: USD > GBP",
                "-1000.00",
                details="GBP",
                balance="260.00",
            ),
            conversion_fee("03/10/2026 09:00:01", "-5.00", "Withdrawal"),
            # A fee with no transfer to belong to.
            conversion_fee("04/10/2026 09:00:00", "-1.00"),
        ],
    )
    report = await ledger.upload(body)
    keys = [k for k in await _ledger(session, ledger)]
    assert sorted(keys) == sorted(
        [
            f"activity:{_key(body, 'Transfer: GBP > USD')}",
            f"activity:{_key(body, 'Transfer: USD > GBP')}",
        ]
    )
    assert await _leg_fees(session) == sorted(
        [
            (f"activity:{_key(body, 'Transfer: GBP > USD')}", "USD", Decimal("6.30")),
            (f"activity:{_key(body, 'Transfer: USD > GBP')}", "USD", Decimal("5.00")),
        ]
    )
    assert "a conversion fee row names no transfer" in report.flagged
    # The fee rows are typed, linked to the transfer they are the fee of.
    rows = await _rows(
        session,
        "select a.amount, t.external_id from finance.etoro_activity a"
        " left join finance.transactions t on t.id = a.transaction_id"
        " where a.connection_id = :c and a.type like '%Conversion Fee' order by a.date",
        c=ledger.connection.id,
    )
    assert [tuple(r) for r in rows] == [
        (Decimal("-6.30"), f"activity:{_key(body, 'Transfer: GBP > USD')}"),
        (Decimal("-5.00"), f"activity:{_key(body, 'Transfer: USD > GBP')}"),
        (Decimal("-1.00"), None),
    ]


async def test_withdraw_request_and_cancel_net_zero(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            activity_row("05/10/2026 09:00:00", "Withdraw Request", "-100.00", balance="1160.00"),
            activity_row(
                "06/10/2026 11:00:00", "Withdraw Request Cancelled", "100.00", balance="1260.00"
            ),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    request = found[f"activity:{_key(body, 'Withdraw Request')}"]
    cancel = found[f"activity:{_key(body, 'Withdraw Request Cancelled')}"]
    # Bank transfers naming no account: Unassigned until the transfer engine pairs them.
    assert request[3:] == (
        None,
        True,
        [
            (TRADING, "USD", Decimal("-100.00"), None, None, False),
            ("Unassigned", "USD", Decimal("100.00"), None, None, False),
        ],
    )
    assert cancel[3:] == (
        None,
        True,
        [
            (TRADING, "USD", Decimal("100.00"), None, None, False),
            ("Unassigned", "USD", Decimal("-100.00"), None, None, False),
        ],
    )
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    assert (await holdings(session, account, NOW))[usd] == Decimal("1260.00")


async def test_withdraw_fee_and_cancel(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            activity_row("05/10/2026 09:00:00", "Withdraw Fee", "-5.00", balance="1255.00"),
            # The refund is its own positive fee row; the cancellation moves nothing.
            activity_row("06/10/2026 11:00:00", "Withdraw Fee", "5.00", balance="1260.00"),
            activity_row(
                "06/10/2026 11:00:00", "Withdraw Fee Cancelled", "5.00", balance="1260.00"
            ),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    fees = sorted(v[5] for k, v in found.items() if v[2] == "Withdrawal fee")
    assert fees == [
        [
            (TRADING, "USD", Decimal("-5.00"), None, None, True),
            ("Unassigned", "USD", Decimal("5.00"), None, None, False),
        ],
        [
            (TRADING, "USD", Decimal("5.00"), None, None, True),
            ("Unassigned", "USD", Decimal("-5.00"), None, None, False),
        ],
    ]
    assert f"activity:{_key(body, 'Withdraw Fee Cancelled')}" not in found
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    assert (await holdings(session, account, NOW))[usd] == Decimal("1260.00")


async def test_dividend_with_withholding_tax(session, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            open_row(),
            activity_row(
                "15/10/2026 12:00:00",
                "Dividend",
                "0.85",
                details=f"{SYMBOL}/USD",
                position_id=POSITION_1,
                balance="760.85",
                asset_type="Stocks",
            ),
            close_row(balance="1280.85"),
        ],
        [closed_row()],
        [dividend_row()],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    dividend = found[f"activity:{_key(body, 'Dividend')}"]
    assert dividend[2] == "Dividend"
    assert dividend[5] == [
        (TRADING, "USD", Decimal("1.00"), None, None, False),
        ("Unassigned", "USD", Decimal("-1.00"), None, None, False),
        (TRADING, "USD", Decimal("-0.15"), None, None, True),
        ("Unassigned", "USD", Decimal("0.15"), None, None, False),
    ]
    [row] = await _rows(
        session,
        "select d.withholding_tax_amount_usd, d.date_of_payment, t.external_id,"
        " i.symbol from finance.etoro_dividends d"
        " join finance.transactions t on t.id = d.transaction_id"
        " join finance.instruments i on i.id = d.finance_instrument_id"
        " where d.connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(row) == (
        Decimal("0.15"),
        date(2026, 10, 15),
        f"activity:{_key(body, 'Dividend')}",
        SYMBOL,
    )
    assert await find_unbalanced_transactions(session) == []


async def test_unknown_activity_type_flagged(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [transfer_in(), activity_row("05/10/2026 09:00:00", "Mystery Bonus", "3.00")],
    )
    report = await ledger.upload(body)
    assert f"{ACCOUNT_ACTIVITY}.Type=Mystery Bonus" in report.flagged
    assert f"activity:{_key(body, 'Mystery Bonus')}" not in await _ledger(session, ledger)
    # Kept typed all the same.
    [kept] = await _rows(
        session,
        "select amount from finance.etoro_activity where connection_id = :c and type = :t",
        c=ledger.connection.id,
        t="Mystery Bonus",
    )
    assert kept.amount == Decimal("3.00")


# Enrichment and precedence (design §5.6-5.7, Decision 14) --------------------------------------


async def test_statement_enriches_api_trade(session, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    api_only = await _ledger(session, ledger)
    body = trade_statement()
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    # The API's postings are unchanged; the statement's transfer is new.
    assert {k: v for k, v in found.items() if not k.startswith("activity:")} == api_only
    assert found == _expected_trade_ledger(body)
    assert await _leg_fees(session) == [
        (f"activity:{_key(body, 'Transfer: GBP > USD')}", "USD", Decimal("6.30"))
    ]
    [trade] = await _rows(
        session,
        "select t.net_profit, t.statement_isin, t.statement_profit_usd,"
        " t.statement_fx_rate_at_open_usd, t.statement_market_spread_usd,"
        " t.statement_close_date, t.statement_row_key is not null as keyed,"
        " t.statement_raw_record_id is not null as linked,"
        " t.raw_record_id is not null as from_api, x.external_id"
        " from finance.etoro_trades t join finance.transactions x on x.id = t.transaction_id"
        " where t.connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(trade) == (
        Decimal("20.00"),
        ISIN,
        Decimal("20.00"),
        "1",
        Decimal("-0.42"),
        datetime(2026, 10, 20, 15, 1, 44, tzinfo=UTC),
        True,
        True,
        True,
        f"close:{POSITION}",
    )
    # The instrument gains the ISIN.
    [instrument] = await _rows(
        session,
        "select array_agg(ii.scheme || '=' || ii.value order by ii.scheme) as ids"
        " from finance.instrument_identifiers ii join finance.instrument_identifiers e"
        " on e.instrument_id = ii.instrument_id and e.scheme = 'etoro_id' and e.value = :v",
        v=str(INSTRUMENT_ID),
    )
    assert instrument.ids == [f"etoro_id={INSTRUMENT_ID}", f"isin={ISIN}"]
    # The open and close rows of the activity link their transactions.
    links = await _rows(
        session,
        "select a.type, t.external_id from finance.etoro_activity a"
        " join finance.transactions t on t.id = a.transaction_id"
        " where a.connection_id = :c and a.position_id is not null order by a.date",
        c=ledger.connection.id,
    )
    assert [tuple(r) for r in links] == [
        ("Open Position", f"open:{POSITION}"),
        ("Position closed", f"close:{POSITION}"),
    ]
    # Account Summary and Financial Summary are typed too.
    [statement] = await _rows(
        session,
        "select username, currency, start_date, transfer_to_trading_from_e_toro_money"
        " from finance.etoro_statements where connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(statement) == (
        USERNAME,
        "USD",
        datetime(2026, 10, 1, tzinfo=UTC),
        Decimal("1250.00"),
    )
    figures = await _rows(
        session,
        "select s.name, v.item_index, v.period_header, v.figure, s.period_from"
        " from finance.etoro_financial_summary s join finance.etoro_financial_summary_values v"
        " using (connection_id, row_key) where s.connection_id = :c order by v.item_index",
        c=ledger.connection.id,
    )
    start = datetime(2026, 10, 1, tzinfo=UTC)
    # By header: a stored row keeps its cells, not their order.
    assert [tuple(r) for r in figures] == [
        ("Dividends", 0, "01/10/2026 - 31/10/2026", Decimal("0.00"), start),
        ("Dividends", 1, "Total", Decimal("0.00"), start),
        ("Dividends", 2, "Year to date", Decimal("0.00"), start),
    ]
    # No review: the two sources agree.
    assert await list_reviews(session, ledger.connection.id) == []
    assert ledger.disagreement_messages() == []


async def test_statement_first_then_api_same_ledger(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = trade_statement()
    await ledger.upload(body)
    # The statement alone posts the trade, at its own times.
    first = await _ledger(session, ledger)
    assert first[f"open:{POSITION}"][0] == datetime(2026, 10, 2, 14, 30, 5, tzinfo=UTC)
    assert first[f"close:{POSITION}"][5] == [
        (TRADING, SYMBOL, Decimal(-4), None, Decimal(130), False),
        (TRADING, "USD", Decimal(520), None, None, False),
    ]
    # The API's later arrival re-posts in place: the same ledger as API first.
    ids = await _rows(
        session,
        "select external_id, id from finance.transactions where connection_id = :c",
        c=ledger.connection.id,
    )
    await ledger.normalize(*BASE, *TRADES, now=NOW + timedelta(days=1))
    assert await _ledger(session, ledger) == _expected_trade_ledger(body)
    after = await _rows(
        session,
        "select external_id, id from finance.transactions where connection_id = :c",
        c=ledger.connection.id,
    )
    assert sorted(map(tuple, after)) == sorted(map(tuple, ids))
    # One instrument, found by the statement's ISIN, which gained eToro's id.
    [instrument] = await _rows(
        session,
        "select array_agg(ii.scheme || '=' || ii.value order by ii.scheme) as ids"
        " from finance.instrument_identifiers ii where ii.instrument_id in"
        " (select instrument_id from finance.instrument_identifiers where value = :v)",
        v=ISIN,
    )
    assert instrument.ids == [f"etoro_id={INSTRUMENT_ID}", f"isin={ISIN}"]
    assert await list_reviews(session, ledger.connection.id) == []


async def test_disagreement_opens_one_review(session, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    await ledger.upload(trade_statement(closed_units="5"))
    [review] = await list_reviews(session, ledger.connection.id)
    assert (review.event_key, review.field, review.api_value, review.statement_value) == (
        f"open:{POSITION}",
        "units",
        "4",
        "5",
    )
    assert review.transaction_id == (await _transaction_id(session, f"open:{POSITION}"))
    [message] = ledger.disagreement_messages()
    assert "···0001" in message and "5" not in message.split("···0001")[1]
    # The API's value posts.
    assert (await _postings(session, f"open:{POSITION}"))[0][2] == Decimal(4)
    # Again: nothing new to review or tell.
    await ledger.upload(trade_statement(closed_units="5"))
    await ledger.normalize(*BASE, *TRADES, now=NOW + timedelta(days=1))
    assert len(await list_reviews(session, ledger.connection.id)) == 1
    assert len(ledger.disagreement_messages()) == 1


async def test_review_closes_when_the_sources_agree(session, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    await ledger.upload(trade_statement(closed_units="5"))
    [review] = await list_reviews(session, ledger.connection.id)
    assert review.resolved_at is None
    # A corrected statement agrees with the API: the item closes on its own, untold.
    await ledger.upload(trade_statement(closed_units="4"))
    [closed] = await list_reviews(session, ledger.connection.id)
    assert (closed.id, closed.resolution, closed.resolved_by) == (
        review.id,
        "api",
        "sources agree",
    )
    assert closed.resolved_at is not None
    assert len(ledger.disagreement_messages()) == 1
    # Not an owner's decision: a new disagreement opens a new item and is told.
    await ledger.upload(trade_statement(closed_units="6"))
    reviews = await list_reviews(session, ledger.connection.id)
    assert [(r.statement_value, r.resolution) for r in reviews] == [("6", None), ("5", "api")]
    assert len(ledger.disagreement_messages()) == 2
    decisions = await session.scalar(
        text("select count(*) from finance.source_decisions where connection_id = :c"),
        {"c": ledger.connection.id},
    )
    assert decisions == 0


async def test_partial_close_no_review(session, ledger) -> None:
    # Four of ten units closed: the "Open Position" row is the whole position's; Closed
    # Positions, as trade history, the closed part's.
    partial = api_trade(investment=Decimal("400.00"), initialInvestment=Decimal("1000.00"))
    partial |= {"netProfit": Decimal("20.00")}
    await ledger.normalize(
        *BASE, ("trade_history", partial), ("order_lookup", lookup_body(ORDER, POSITION))
    )
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            open_row() | {"Amount": "1000", "Units / Contracts": "10", "Balance": "260.00"},
            close_row(balance="680.00") | {"Amount": "420.00"},
        ],
        [closed_row() | {"Amount": "400"}],
    )
    await ledger.upload(body)
    assert await list_reviews(session, ledger.connection.id) == []
    assert (await _postings(session, f"open:{POSITION}"))[:2] == [
        (TRADING, SYMBOL, Decimal(4), Decimal(100), "USD", None, None, False),
        (TRADING, "USD", Decimal("-400.00"), None, None, None, None, False),
    ]


async def test_resolve_toward_statement_reposts_and_replays(session, run, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    await ledger.upload(trade_statement(closed_units="5"))
    [review] = await list_reviews(session, ledger.connection.id)
    await run(lambda ctx: resolve_review(ctx, review.id, "statement"))
    # The next sync takes the statement's units.
    await ledger.normalize(*BASE, now=NOW + timedelta(days=1))
    assert (await _postings(session, f"open:{POSITION}"))[:2] == [
        (TRADING, SYMBOL, Decimal(5), Decimal(100), "USD", None, None, False),
        (TRADING, "USD", Decimal("-500.00"), None, None, None, None, False),
    ]
    before = await _ledger(session, ledger)
    # Regeneration replays the owner's decision.
    await _delete_derived(session, ledger.connection)
    await ledger.replay()
    assert await _ledger(session, ledger) == before
    [closed] = await list_reviews(session, ledger.connection.id)
    assert closed.resolution == "statement"
    assert len(ledger.disagreement_messages()) == 1
    # A later change in either source's value reopens it, and the API's value posts again.
    await ledger.upload(trade_statement(closed_units="6"))
    reviews = await list_reviews(session, ledger.connection.id)
    assert [(r.statement_value, r.resolution) for r in reviews] == [
        ("6", None),
        ("5", "statement"),
    ]
    assert (await _postings(session, f"open:{POSITION}"))[0][2] == Decimal(4)
    assert len(ledger.disagreement_messages()) == 2


async def test_overlapping_statements_write_once(session, ledger) -> None:
    await ledger.normalize(*BASE)

    def rows(days: range) -> list[dict[str, str]]:
        return [
            activity_row(
                f"{day:02d}/10/2026 09:00:00",
                "Transfer: GBP > USD",
                f"{day}.00",
                details="GBP",
                balance=f"{sum(range(1, day + 1))}.00",
            )
            for day in days
        ]

    first = build_statement(summary(username=USERNAME), rows(range(1, 13)))
    second = build_statement(
        summary(
            username=USERNAME,
            start="03/10/2026 00:00:00",
            end="15/10/2026 23:59:59",
        ),
        rows(range(3, 16)),
    )
    created = await ledger.upload(first)
    assert (created.created, created.updated) == (12, 0)
    # The same statement again: every event as it was.
    again = await ledger.upload(first)
    assert (again.created, again.updated, again.unchanged) == (0, 0, 12)
    # An overlapping one shares ten rows: only its three new ones are written.
    overlap = await ledger.upload(second)
    assert (overlap.created, overlap.updated, overlap.unchanged) == (3, 0, 10)
    count = await session.scalar(
        text(
            "select count(*) from finance.transactions where connection_id = :c"
            " and external_id like 'activity:%'"
        ),
        {"c": ledger.connection.id},
    )
    assert count == 15
    typed = await session.scalar(
        text("select count(*) from finance.etoro_activity where connection_id = :c"),
        {"c": ledger.connection.id},
    )
    assert typed == 15


async def test_isin_merges_with_existing_instrument(session, run, ledger) -> None:
    usd = await get_currency(session, "USD")
    held = await run(
        lambda ctx: create_instrument(
            ctx,
            kind="equity",
            symbol="ZZQA.IB",
            name="Synthetic Quality A",
            decimals=4,
            quote_instrument_id=usd.id,
            identifiers=[InstrumentIdentifier("isin", ISIN)],
        )
    )
    await ledger.normalize(*BASE, *TRADES)
    assert (await _postings(session, f"open:{POSITION}"))[0][1] == SYMBOL
    await ledger.upload(trade_statement())
    # The eToro instrument is merged into the one that held the ISIN.
    assert (await _postings(session, f"open:{POSITION}"))[0][1] == "ZZQA.IB"
    [instrument] = await _rows(
        session,
        "select i.id, i.decimals, array_agg(ii.scheme order by ii.scheme) as schemes"
        " from finance.instruments i join finance.instrument_identifiers ii"
        " on ii.instrument_id = i.id where ii.instrument_id in (select instrument_id"
        " from finance.instrument_identifiers where scheme = 'etoro_id' and value = :v)"
        " group by i.id",
        v=str(INSTRUMENT_ID),
    )
    assert (instrument.id, instrument.decimals, instrument.schemes) == (
        held.id,
        6,
        ["etoro_id", "isin"],
    )
    # The next sync finds it by eToro's id: nothing changes.
    report = await ledger.normalize(*BASE, *TRADES, now=NOW + timedelta(days=1))
    assert (report.created, report.updated) == (0, 0)


async def test_account_summary_keeps_both_names_and_column_c(session, ledger) -> None:
    await ledger.normalize(*BASE)
    sheets = statement_sheets(summary(username=USERNAME), [transfer_in()])
    name, rows = sheets[0]
    assert name == ACCOUNT_SUMMARY
    rows = list(rows)
    rows.insert(2, [("shared", "Name"), ("inline", "Synthetic Q. Trader")])
    # Column C of a figure's row.
    rows = [[*row, ("inline", "N/A")] if row[0] == ("shared", "Deposits") else row for row in rows]
    sheets[0] = (name, rows)
    await ledger.upload(build_workbook(sheets))
    [row] = await _rows(
        session,
        "select name, name_occurrence_1, deposits, deposits_2 from finance.etoro_statements"
        " where connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(row) == ("Synthetic Trader", "Synthetic Q. Trader", Decimal(0), "N/A")


# Fees the close and the statement both name (Task 10's review) ----------------------------------


async def test_close_fees_left_to_the_statement_rows(session, ledger) -> None:
    # Trade history's `fees` are a position's overnight fees and dividends (PnL's `totalFees`:
    # "overnight fees and dividends ... negative amount represents refund"); `netProfit` is
    # the close's profit, as Closed Positions' Profit(USD), which leaves them out; the
    # statement posts each as its own row. So the close posts none of them.
    await ledger.normalize(
        *BASE,
        ("trade_history", api_trade(fees=Decimal("-0.55"))),
        ("order_lookup", lookup_body(ORDER, POSITION)),
    )
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            open_row(),
            activity_row(
                "15/10/2026 12:00:00",
                "Dividend",
                "0.85",
                position_id=POSITION_1,
                balance="760.85",
            ),
            activity_row(
                "16/10/2026 12:00:00",
                "Overnight fee",
                "-0.30",
                position_id=POSITION_1,
                balance="760.55",
            ),
            close_row(balance="1280.55"),
        ],
        [closed_row() | {"Overnight Fees and Dividends": "0.55", "Spread Fees (USD)": "0"}],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    assert found[f"close:{POSITION}"][5] == [
        (TRADING, SYMBOL, Decimal(-4), None, Decimal(130), False),
        (TRADING, "USD", Decimal(520), None, None, False),
    ]
    assert found[f"activity:{_key(body, 'Overnight fee')}"][5] == [
        (TRADING, "USD", Decimal("-0.30"), None, None, True),
        ("Unassigned", "USD", Decimal("0.30"), None, None, False),
    ]
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    assert (await holdings(session, account, NOW))[usd] == Decimal("1280.55")
    assert await list_reviews(session, ledger.connection.id) == []
    # The fees stay typed.
    fees = await session.scalar(
        text("select fees from finance.etoro_trades where connection_id = :c"),
        {"c": ledger.connection.id},
    )
    assert fees == Decimal("-0.55")


# The ledger's cash is the statement's balance, per second (design §5.7) ------------------------


async def test_trading_cash_equals_statement_balance_each_second(session, ledger) -> None:
    await ledger.normalize(*BASE)
    activity = [
        transfer_in(),
        conversion_fee("01/10/2026 10:00:00"),
        open_row(),
        # One second's rows out of balance order: the fee's row shows the balance after both.
        activity_row("05/10/2026 09:00:00", "Withdraw Fee", "-5.00", balance="655.00"),
        activity_row("05/10/2026 09:00:00", "Withdraw Request", "-100.00", balance="660.00"),
        activity_row(
            "06/10/2026 11:00:00", "Withdraw Request Cancelled", "100.00", balance="755.00"
        ),
        activity_row("06/10/2026 11:00:00", "Withdraw Fee", "5.00", balance="760.00"),
        activity_row("06/10/2026 11:00:00", "Withdraw Fee Cancelled", "5.00", balance="760.00"),
        activity_row(
            "15/10/2026 12:00:00", "Dividend", "0.85", position_id=POSITION_1, balance="760.85"
        ),
        activity_row(
            "16/10/2026 12:00:00",
            "Overnight fee",
            "-0.30",
            position_id=POSITION_1,
            balance="760.55",
        ),
        close_row(balance="1280.55"),
        activity_row(
            "25/10/2026 10:00:00",
            "Transfer: USD > GBP",
            "-1000.00",
            details="GBP",
            balance="280.55",
        ),
        conversion_fee("25/10/2026 10:00:00", "-5.00", "Withdrawal"),
    ]
    await ledger.upload(
        build_statement(summary(username=USERNAME), activity, [closed_row()], [dividend_row()])
    )
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    by_second: dict[datetime, set[Decimal]] = {}
    for row in activity:
        day, moment = row["Date"].split(" ")
        d, m, y = (int(p) for p in day.split("/"))
        hh, mm, ss = (int(p) for p in moment.split(":"))
        at = datetime(y, m, d, hh, mm, ss, tzinfo=UTC)
        by_second.setdefault(at, set()).add(Decimal(row["Balance"]))
    for at, balances in sorted(by_second.items()):
        cash = (await holdings(session, account, at + timedelta(microseconds=999999))).get(
            usd, Decimal(0)
        )
        assert cash in balances, at
    assert (await holdings(session, account, NOW))[usd] == Decimal("280.55")
    assert await find_unbalanced_transactions(session) == []


# Helpers ------------------------------------------------------------------------------------


async def _account(session, name: str) -> uuid.UUID:
    return (
        await session.execute(text("select id from finance.accounts where name = :n"), {"n": name})
    ).scalar_one()


async def _transaction_id(session, external_id: str) -> uuid.UUID:
    return (
        await session.execute(
            text("select id from finance.transactions where external_id = :e"), {"e": external_id}
        )
    ).scalar_one()
