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
from sqlalchemy import select, text

from lararium.core import models as core_models
from lararium.core.connections import get_connection
from lararium.core.documents import LocalDocumentStore
from lararium.modules.finance import source_merge
from lararium.modules.finance.connector_run import holdings
from lararium.modules.finance.etoro import statement_ledger
from lararium.modules.finance.etoro import sync as etoro_sync
from lararium.modules.finance.etoro.fields import ACCOUNT_ACTIVITY, ACCOUNT_SUMMARY
from lararium.modules.finance.etoro.merge import FIELDS, TOGETHER, TradeEvent
from lararium.modules.finance.etoro.normalize import EtoroDataError, NormalizeReport, normalize
from lararium.modules.finance.etoro.statement import parse_statement
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    create_instrument,
    get_currency,
)
from lararium.modules.finance.ledger import find_unbalanced_transactions, void_transaction
from lararium.modules.finance.source_reviews import list_reviews, resolve_review
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers import find_transfer_violations
from lararium.modules.finance.transfers.contra import snapshot_own_records

from .fixtures import (
    CASH_ACCOUNT,
    INSTRUMENT_ID,
    SYMBOL,
    aggregate_body,
    balances_body,
    instrument_meta,
    lookup_body,
    me_json,
    pnl_body,
    pnl_position,
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
from .test_normalize_api import (
    Ledger,
    _connection,
    _delete_derived,
    _ledger_state,
    _postings,
    _rows,
)

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


def dividend_activity() -> dict[str, str]:
    return activity_row(
        "15/10/2026 12:00:00",
        "Dividend",
        "0.85",
        details=f"{SYMBOL}/USD",
        position_id=POSITION_1,
        balance="760.85",
        asset_type="Stocks",
    )


def trade_statement(
    closed_units: str = "4",
    closed_profit: str = "20.00",
    close_change: str = "20.00",
    *,
    dividend: bool = False,
) -> bytes:
    """A statement of one transfer in (with its fee) and one position opened and closed;
    `closed_units` and `closed_profit` what Closed Positions says of it, `close_change` the
    close row's Realized Equity Change; with `dividend`, a dividend (its withholding tax in
    the Dividends sheet) while the position was open."""
    return build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            conversion_fee("01/10/2026 10:00:00"),
            open_row(),
            *([dividend_activity()] if dividend else []),
            close_row() | {"Realized Equity Change": close_change},
        ],
        [closed_row() | {"Units / Contracts": closed_units, "Profit(USD)": closed_profit}],
        [dividend_row()] if dividend else [],
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
    description, counterparty identifier, bank transfer, postings, the external id of the
    transaction it cancels), postings as (account, symbol, quantity, cost, price, is_fee) with
    exact numbers, no UUIDs."""
    rows = await _rows(
        session,
        "select t.id, t.external_id, t.occurred_at, t.booking_date, t.description,"
        " ci.value as counterparty, t.bank_transfer, ct.external_id as cancels"
        " from finance.transactions t"
        " left join finance.account_identifiers ci on ci.id = t.counterparty_identifier_id"
        " left join finance.transactions ct on ct.id = t.cancels_transaction_id"
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
            row.cancels,
        )
    return found


def _key(body: bytes, type_: str) -> str:
    """The statement row's key (Decision 10), as the reader gives it."""
    statement = parse_statement(body, etoro_sync.STATEMENT_ZONE)
    [key] = [
        r.key for r in statement.rows if r.sheet == ACCOUNT_ACTIVITY and r.cells["Type"] == type_
    ]
    return key


def _keys(body: bytes, type_: str) -> list[str]:
    """The keys of the statement's Account Activity rows of the Type, in the sheet's order."""
    statement = parse_statement(body, etoro_sync.STATEMENT_ZONE)
    return [
        r.key for r in statement.rows if r.sheet == ACCOUNT_ACTIVITY and r.cells["Type"] == type_
    ]


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
            None,
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
            None,
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
            None,
        ),
    }


async def _review_links(session, ledger: Ledger) -> list[tuple]:
    """(event key, field, API value, statement value, resolution, linked external id)."""
    rows = await _rows(
        session,
        "select r.event_key, r.field, r.api_value, r.statement_value, r.resolution,"
        " t.external_id from finance.source_reviews r"
        " left join finance.transactions t on t.id = r.transaction_id"
        " where r.connection_id = :c order by r.opened_at, r.field",
        c=ledger.connection.id,
    )
    return [tuple(r) for r in rows]


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
        "describes": "opened",
    } | overrides
    return TradeEvent(**values)


def test_posting_values_and_disagreements() -> None:
    api = _event(cash=Decimal("500.0"))
    same = _event(occurred_at=datetime(2026, 10, 2, 14, 30, 6, tzinfo=UTC))
    # Equal numbers in another scale, and another second of the same day, agree.
    assert source_merge.disagreements(FIELDS, api, same) == []
    statement = _event(
        units=Decimal(5),
        profit=Decimal("1.5"),
        booking_date=date(2026, 10, 3),
        occurred_at=datetime(2026, 10, 3, 0, 30, tzinfo=UTC),
    )
    # A profit only one source states is not a disagreement.
    assert source_merge.disagreements(FIELDS, api, statement) == [
        ("units", "4", "5"),
        ("booking_date", "2026-10-02", "2026-10-03"),
    ]
    assert source_merge.posting_values(api, statement, FIELDS, {}, TOGETHER) == api
    assert source_merge.posting_values(None, statement, FIELDS, {}, TOGETHER) == statement
    assert source_merge.posting_values(api, None, FIELDS, {"units": "statement"}, TOGETHER) == api
    taken = source_merge.posting_values(
        api, statement, FIELDS, {"units": "statement", "booking_date": "statement"}, TOGETHER
    )
    assert taken == dataclasses.replace(
        api, units=Decimal(5), booking_date=date(2026, 10, 3), occurred_at=statement.occurred_at
    )
    with pytest.raises(ValueError):
        source_merge.posting_values(None, None, FIELDS, {}, TOGETHER)
    # Agreeing is stated by both and equal: a profit one source leaves out agrees on nothing.
    assert source_merge.agreements(FIELDS, api, statement) == ["cash"]
    assert source_merge.agreements(FIELDS, api, same) == ["units", "cash", "booking_date"]


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
        None,
    )
    assert found[f"activity:{_key(body, 'Transfer: USD > GBP')}"][2:] == (
        "Transfer to eToro Money",
        CASH_ACCOUNT,
        False,
        [
            (TRADING, "USD", Decimal("-1000.00"), None, None, False),
            ("In transit", "USD", Decimal("1000.00"), None, None, False),
        ],
        None,
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


def _withdrawal(at: str) -> dict[str, str]:
    return activity_row(at, "Withdraw Request", "-100.00")


def _cancellation(at: str, amount: str = "100.00", **cells: str) -> dict[str, str]:
    return activity_row(at, "Withdraw Request Cancelled", amount, **cells)


async def _violations(session) -> list[str]:
    """The external ids of the transfer invariant's violations, but the transfer in from eToro
    Money, whose transfer the engine (not run here) records."""
    found = await find_transfer_violations(session)
    rows = await _rows(
        session,
        "select external_id from finance.transactions where id = any(:ids)"
        " and description <> 'Transfer from eToro Money' order by 1",
        ids=found,
    )
    return [row.external_id for row in rows]


def _against(entry: tuple) -> str:
    """The account a cash event's contra (its second posting) is on."""
    return entry[5][1][0]


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
    request_key = f"activity:{_key(body, 'Withdraw Request')}"
    request = found[request_key]
    cancel = found[f"activity:{_key(body, 'Withdraw Request Cancelled')}"]
    # A cancelled pair: both against In transit, which they leave level, the cancellation
    # naming its request; neither is spending or income.
    assert request[3:] == (
        None,
        True,
        [
            (TRADING, "USD", Decimal("-100.00"), None, None, False),
            ("In transit", "USD", Decimal("100.00"), None, None, False),
        ],
        None,
    )
    assert cancel[3:] == (
        None,
        True,
        [
            (TRADING, "USD", Decimal("100.00"), None, None, False),
            ("In transit", "USD", Decimal("-100.00"), None, None, False),
        ],
        request_key,
    )
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    assert (await holdings(session, account, NOW))[usd] == Decimal("1260.00")
    assert await _violations(session) == []


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
    fee, refund = (f"activity:{key}" for key in _keys(body, "Withdraw Fee"))
    assert (found[fee][2], found[fee][5:]) == (
        "Withdrawal fee",
        (
            [
                (TRADING, "USD", Decimal("-5.00"), None, None, True),
                ("In transit", "USD", Decimal("5.00"), None, None, False),
            ],
            None,
        ),
    )
    assert (found[refund][2], found[refund][5:]) == (
        "Withdrawal fee",
        (
            [
                (TRADING, "USD", Decimal("5.00"), None, None, True),
                ("In transit", "USD", Decimal("-5.00"), None, None, False),
            ],
            fee,
        ),
    )
    assert f"activity:{_key(body, 'Withdraw Fee Cancelled')}" not in found
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    assert (await holdings(session, account, NOW))[usd] == Decimal("1260.00")
    assert await _violations(session) == []


async def test_a_refund_needs_its_cancelled_row(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            activity_row("05/10/2026 09:00:00", "Withdraw Fee", "-5.00"),
            activity_row("06/10/2026 11:00:00", "Withdraw Fee", "5.00"),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    for key in _keys(body, "Withdraw Fee"):
        assert (_against(found[f"activity:{key}"]), found[f"activity:{key}"][6]) == (
            "Unassigned",
            None,
        )


@pytest.mark.parametrize(("refunded_at", "paired"), [("11:00:01", True), ("11:00:02", False)])
async def test_a_refund_one_second_from_its_cancelled_row(
    session, ledger, refunded_at, paired
) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            activity_row("05/10/2026 09:00:00", "Withdraw Fee", "-5.00"),
            activity_row("06/10/2026 11:00:00", "Withdraw Fee Cancelled", "5.00"),
            activity_row(f"06/10/2026 {refunded_at}", "Withdraw Fee", "5.00"),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    fee, refund = (f"activity:{key}" for key in _keys(body, "Withdraw Fee"))
    if paired:
        assert (_against(found[fee]), found[fee][6]) == ("In transit", None)
        assert (_against(found[refund]), found[refund][6]) == ("In transit", fee)
    else:
        assert (_against(found[fee]), found[fee][6]) == ("Unassigned", None)
        assert (_against(found[refund]), found[refund][6]) == ("Unassigned", None)


async def _split_pair(session, ledger, *, request_first: bool) -> None:
    """A request and its cancellation in two statements: the pair once both are uploaded,
    the request keeping its transaction."""
    await ledger.normalize(*BASE)
    with_request = build_statement(
        summary(username=USERNAME), [transfer_in(), _withdrawal("05/10/2026 09:00:00")]
    )
    with_cancellation = build_statement(
        summary(username=USERNAME), [_cancellation("06/10/2026 11:00:00")]
    )
    request = f"activity:{_key(with_request, 'Withdraw Request')}"
    cancel = f"activity:{_key(with_cancellation, 'Withdraw Request Cancelled')}"
    first, alone = (request, with_request) if request_first else (cancel, with_cancellation)
    await ledger.upload(alone)
    found = await _ledger(session, ledger)
    # Alone, it posts as any bank transfer with no counterpart yet.
    assert (_against(found[first]), found[first][4], found[first][6]) == (
        "Unassigned",
        True,
        None,
    )
    posted = await _transaction_id(session, first)
    await ledger.upload(with_cancellation if request_first else with_request)
    found = await _ledger(session, ledger)
    assert (_against(found[request]), found[request][6]) == ("In transit", None)
    assert (_against(found[cancel]), found[cancel][6]) == ("In transit", request)
    assert await _transaction_id(session, first) == posted
    assert await _violations(session) == []


async def test_a_cancellation_in_a_later_statement(session, ledger) -> None:
    await _split_pair(session, ledger, request_first=True)


async def test_a_request_uploaded_after_its_cancellation(session, ledger) -> None:
    await _split_pair(session, ledger, request_first=False)


async def test_a_cancellation_with_no_request(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME), [transfer_in(), _cancellation("06/10/2026 11:00:00")]
    )
    await ledger.upload(body)
    cancel = (await _ledger(session, ledger))[
        f"activity:{_key(body, 'Withdraw Request Cancelled')}"
    ]
    assert (_against(cancel), cancel[6]) == ("Unassigned", None)


async def test_the_later_of_two_requests_is_the_cancelled_one(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            _withdrawal("05/10/2026 09:00:00"),
            _withdrawal("05/10/2026 10:00:00"),
            _cancellation("05/10/2026 11:00:00"),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    earlier, later = (f"activity:{key}" for key in _keys(body, "Withdraw Request"))
    cancel = f"activity:{_key(body, 'Withdraw Request Cancelled')}"
    assert (_against(found[earlier]), found[earlier][6]) == ("Unassigned", None)
    assert (_against(found[later]), found[later][6]) == ("In transit", None)
    assert (_against(found[cancel]), found[cancel][6]) == ("In transit", later)


async def test_a_second_cancellation_cancels_nothing(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            _withdrawal("05/10/2026 09:00:00"),
            _cancellation("05/10/2026 11:00:00"),
            _cancellation("05/10/2026 12:00:00"),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    request = f"activity:{_key(body, 'Withdraw Request')}"
    first, second = (f"activity:{key}" for key in _keys(body, "Withdraw Request Cancelled"))
    assert (_against(found[first]), found[first][6]) == ("In transit", request)
    assert (_against(found[second]), found[second][6]) == ("Unassigned", None)


async def test_a_request_after_the_cancellation_is_a_withdrawal(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            _withdrawal("05/10/2026 09:00:00"),
            _cancellation("05/10/2026 11:00:00"),
            _withdrawal("05/10/2026 12:00:00"),
        ],
    )
    await ledger.upload(body)
    found = await _ledger(session, ledger)
    cancelled, withdrawn = (f"activity:{key}" for key in _keys(body, "Withdraw Request"))
    assert _against(found[cancelled]) == "In transit"
    assert (_against(found[withdrawn]), found[withdrawn][4], found[withdrawn][6]) == (
        "Unassigned",
        True,
        None,
    )
    withdrawn_id = await session.scalar(
        text("select id from finance.transactions where connection_id = :c and external_id = :e"),
        {"c": ledger.connection.id, "e": withdrawn},
    )
    own = await snapshot_own_records(session, await system_account_ids(session))
    assert withdrawn_id in set(await session.scalars(select(own.c.transaction_id)))


async def test_a_cancellation_without_a_number_cancels_nothing(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            _withdrawal("05/10/2026 09:00:00"),
            _cancellation("05/10/2026 11:00:00", change="-"),
        ],
    )
    report = await ledger.upload(body)
    found = await _ledger(session, ledger)
    request = found[f"activity:{_key(body, 'Withdraw Request')}"]
    assert (_against(request), request[6]) == ("Unassigned", None)
    assert f"activity:{_key(body, 'Withdraw Request Cancelled')}" not in found
    assert report.flagged == {f"{ACCOUNT_ACTIVITY}.Realized Equity Change has no number"}


async def test_a_side_the_owner_voided_ends_the_pair(session, run, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            _withdrawal("05/10/2026 09:00:00"),
            _cancellation("06/10/2026 11:00:00"),
        ],
    )
    await ledger.upload(body)
    request = f"activity:{_key(body, 'Withdraw Request')}"
    cancel = f"activity:{_key(body, 'Withdraw Request Cancelled')}"
    assert _against((await _ledger(session, ledger))[request]) == "In transit"
    await run(void_transaction, await _transaction_id(session, cancel))
    for _ in range(2):
        report = await ledger.replay()
        found = await _ledger(session, ledger)
        assert cancel not in found
        assert (_against(found[request]), found[request][4], found[request][6]) == (
            "Unassigned",
            True,
            None,
        )
    assert (report.created, report.updated) == (0, 0)


async def _in_two_statements(
    session, ledger, first: list[dict[str, str]], second: list[dict[str, str]]
) -> dict[str, tuple]:
    """The ledger after uploading the two statements in turn, which is the one a single
    statement of all their rows gives: replaying them together (as one run) changes nothing,
    and regenerating the ledger from them in one run gives it again. The transfer invariant
    holds without heal."""
    await ledger.normalize(*BASE)
    await ledger.upload(build_statement(summary(username=USERNAME), [transfer_in(), *first]))
    await ledger.upload(build_statement(summary(username=USERNAME), second))
    found = await _ledger(session, ledger)
    assert await _violations(session) == []
    replayed = await ledger.replay()
    assert await _ledger(session, ledger) == found
    assert replayed.updated == 0
    await _delete_derived(session, ledger.connection)
    await ledger.replay()
    assert await _ledger(session, ledger) == found
    return found


def _activity_key(cells: dict[str, str]) -> str:
    """The transaction key of one row, as the statement holding only it gives it."""
    body = build_statement(summary(username=USERNAME), [cells])
    return f"activity:{_key(body, cells['Type'])}"


async def test_a_middle_statement_uploaded_last_moves_the_pair(session, ledger) -> None:
    earlier, later = _withdrawal("05/10/2026 09:00:00"), _withdrawal("05/10/2026 10:00:00")
    cancellation = _cancellation("05/10/2026 11:00:00")
    found = await _in_two_statements(session, ledger, [earlier, cancellation], [later])
    r1, r2, cancel = (_activity_key(cells) for cells in (earlier, later, cancellation))
    assert (_against(found[r1]), found[r1][6]) == ("Unassigned", None)
    assert (_against(found[r2]), found[r2][6]) == ("In transit", None)
    assert (_against(found[cancel]), found[cancel][6]) == ("In transit", r2)


async def test_an_earlier_cancellation_uploaded_last(session, ledger) -> None:
    request = _withdrawal("05/10/2026 09:00:00")
    earlier, later = _cancellation("05/10/2026 10:00:00"), _cancellation("05/10/2026 12:00:00")
    found = await _in_two_statements(session, ledger, [request, later], [earlier])
    r, c0, c = (_activity_key(cells) for cells in (request, earlier, later))
    assert (_against(found[r]), found[r][6]) == ("In transit", None)
    assert (_against(found[c0]), found[c0][6]) == ("In transit", r)
    assert (_against(found[c]), found[c][6]) == ("Unassigned", None)


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
        "select username, currency, account_currency, second_currency, start_date,"
        " transfer_to_trading_from_e_toro_money"
        " from finance.etoro_statements where connection_id = :c",
        c=ledger.connection.id,
    )
    assert tuple(statement) == (
        USERNAME,
        "USD/GBP",
        "USD",
        "GBP",
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


class _FailingOnce:
    """A notifier whose first send fails, as an unreachable webhook would."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.failed = False

    async def send(self, text: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("unreachable")
        self.messages.append(text)


async def test_a_failed_review_notification_is_sent_next_run(session, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    ledger.notifier = _FailingOnce(ledger.notifier.messages)
    await ledger.upload(trade_statement(closed_units="5"))
    assert ledger.disagreement_messages() == []

    await ledger.normalize(*BASE, *TRADES, now=NOW + timedelta(days=1))
    [message] = ledger.disagreement_messages()
    assert "···0001" in message and "resolved through the API" in message
    await ledger.normalize(*BASE, *TRADES, now=NOW + timedelta(days=2))
    assert len(ledger.disagreement_messages()) == 1


async def test_a_close_without_its_lookup_posts_once_the_statement_settles_it(
    session, ledger
) -> None:
    """The API states no settlement for a closed trade whose order lookup is missing (a 404,
    or no orderId): the statement's Closed Positions row settles it, so it posts and the
    flag clears (as test_settlement_unknown_has_its_own_flag's lookup does)."""
    report = await ledger.normalize(*BASE, ("trade_history", api_trade()))
    assert await _postings(session, f"close:{POSITION}") == []
    assert "position settlement unknown (1)" in report.flags_in_full

    report = await ledger.upload(trade_statement())

    assert await _postings(session, f"close:{POSITION}") != []
    assert await _postings(session, f"open:{POSITION}") != []
    assert not any(f.startswith("position settlement unknown") for f in report.flags_in_full)


@pytest.mark.parametrize("close_listed", [True, False])
async def test_a_statement_close_for_nothing_sends_its_shares_to_unassigned(
    session, ledger, close_listed
) -> None:
    """A total loss only the statement has, with or without its "Position closed" row
    (without it, Closed Positions' amount and profit give the cash): the close moves none."""
    await ledger.normalize(*BASE)
    lost = close_row(balance="760.00") | {
        "Amount": "0",
        "Realized Equity Change": "-500.00",
        "Realized Equity": "760.00",
    }
    body = build_statement(
        summary(username=USERNAME),
        [transfer_in(), open_row(), *([lost] if close_listed else [])],
        [closed_row() | {"Profit(USD)": "-500.00", "Close Rate": "0"}],
    )

    await ledger.upload(body)

    postings = await _postings(session, f"close:{POSITION}")
    assert postings == [
        (TRADING, SYMBOL, Decimal(-4), None, None, None, None, False),
        ("Unassigned", SYMBOL, Decimal(4), None, None, None, None, False),
    ]
    held = await _rows(
        session,
        "select i.symbol, sum(p.quantity) as q from finance.postings p"
        " join finance.transactions t on t.id = p.transaction_id"
        " join finance.accounts a on a.id = p.account_id"
        " join finance.instruments i on i.id = p.instrument_id"
        " where a.name = :a and t.status <> 'void' group by i.symbol having sum(p.quantity) <> 0",
        a=TRADING,
    )
    assert [tuple(r) for r in held] == [("USD", Decimal("760.00"))]
    again = await ledger.upload(body)
    assert (again.created, again.updated) == (0, 0)
    assert await _postings(session, f"close:{POSITION}") == postings


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
    await ledger.upload(trade_statement(closed_units="5", dividend=True))
    [review] = await list_reviews(session, ledger.connection.id)
    await run(lambda ctx: resolve_review(ctx, review.id, "statement"))
    # The next sync takes the statement's units.
    await ledger.normalize(*BASE, now=NOW + timedelta(days=1))
    assert (await _postings(session, f"open:{POSITION}"))[:2] == [
        (TRADING, SYMBOL, Decimal(5), Decimal(100), "USD", None, None, False),
        (TRADING, "USD", Decimal("-500.00"), None, None, None, None, False),
    ]
    before = await _ledger(session, ledger)
    state = await _ledger_state(session, ledger.connection)
    fees = await _leg_fees(session)
    links = await _review_links(session, ledger)
    assert links == [(f"open:{POSITION}", "units", "4", "5", "statement", f"open:{POSITION}")]
    assert fees != []
    # The replay's comparison covers a dividend's typed row, linked to its posting.
    assert [
        tuple(r)
        for r in await _rows(
            session,
            "select d.withholding_tax_amount_usd, t.external_id is not null as linked"
            " from finance.etoro_dividends d"
            " left join finance.transactions t on t.id = d.transaction_id"
            " where d.connection_id = :c",
            c=ledger.connection.id,
        )
    ] == [(Decimal("0.15"), True)]
    # Regeneration replays the owner's decision, the statement's typed rows, the leg fee, and
    # the review's link to its transaction.
    await _delete_derived(session, ledger.connection)
    await ledger.replay()
    assert await _ledger(session, ledger) == before
    assert await _ledger_state(session, ledger.connection) == state
    assert await _leg_fees(session) == fees
    assert await _review_links(session, ledger) == links
    [closed] = await list_reviews(session, ledger.connection.id)
    assert closed.resolution == "statement"
    assert closed.transaction_id == await _transaction_id(session, f"open:{POSITION}")
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
        conversion_fee("01/10/2026 10:00:00") | {"Balance": "1260.00"},
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
        conversion_fee("25/10/2026 10:00:00", "-5.00", "Withdrawal") | {"Balance": "280.55"},
    ]
    await ledger.upload(
        build_statement(summary(username=USERNAME), activity, [closed_row()], [dividend_row()])
    )
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    by_second: dict[datetime, list[dict[str, str]]] = {}
    for cells in activity:
        moment = datetime.strptime(cells["Date"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=UTC)
        by_second.setdefault(moment, []).append(cells)
    closing = Decimal(0)
    for at, rows in sorted(by_second.items()):
        # The balance after the second: the one before plus the cash its rows move (design
        # §5.7), which is one of the second's Balances, whatever order its rows are listed in.
        closing += sum((statement_ledger.cash(_row(cells)) for cells in rows), Decimal(0))
        assert closing in {Decimal(cells["Balance"]) for cells in rows}, at
        cash = (await holdings(session, account, at + timedelta(microseconds=999999))).get(
            usd, Decimal(0)
        )
        assert cash == closing, at
    assert closing == Decimal("280.55")
    assert await find_unbalanced_transactions(session) == []


def _row(cells: dict[str, str]) -> statement_ledger.Row:
    """An Account Activity row as the normalizer reads it."""
    return statement_ledger.Row(uuid.uuid4(), ACCOUNT_ACTIVITY, "0" * 32, cells, NOW, NOW, "", NOW)


# Review fixes -------------------------------------------------------------------------------


@pytest.mark.parametrize("statement_first", [True, False])
async def test_disagreement_the_same_in_either_order(session, ledger, statement_first) -> None:
    body = trade_statement(closed_units="5")
    if statement_first:
        await ledger.normalize(*BASE)
        await ledger.upload(body)
        await ledger.normalize(*BASE, *TRADES, now=NOW + timedelta(days=1))
    else:
        await ledger.normalize(*BASE, *TRADES)
        await ledger.upload(body)
    assert await _review_links(session, ledger) == [
        (f"open:{POSITION}", "units", "4", "5", None, f"open:{POSITION}")
    ]
    assert len(ledger.disagreement_messages()) == 1
    assert await _leg_fees(session) == [
        (f"activity:{_key(body, 'Transfer: GBP > USD')}", "USD", Decimal("6.30"))
    ]
    # The API's value posts either way.
    assert (await _postings(session, f"open:{POSITION}"))[0][2] == Decimal(4)


NO_CURRENCY = [entry for entry in BASE if entry[0] != "aggregate"]


@pytest.mark.parametrize("statement_first", [True, False])
async def test_statement_names_the_currency_the_api_lacks(session, ledger, statement_first) -> None:
    # Neither the aggregate nor the breakdown gives the instrument's currency: the
    # statement's activity does ("ZZQA/USD"), so the API's trade posts once it is uploaded.
    body = trade_statement()
    if statement_first:
        await ledger.normalize(*NO_CURRENCY)
        await ledger.upload(body)
        await ledger.normalize(*NO_CURRENCY, *TRADES, now=NOW + timedelta(days=1))
    else:
        await ledger.normalize(*NO_CURRENCY, *TRADES)
        assert f"open:{POSITION}" not in await _ledger(session, ledger)
        await ledger.upload(body)
    assert await _ledger(session, ledger) == _expected_trade_ledger(body)
    account = await _account(session, TRADING)
    usd = (await get_currency(session, "USD")).id
    assert (await holdings(session, account, NOW + timedelta(days=2)))[usd] == Decimal("1280.00")


async def test_open_and_close_naming_different_currencies_flagged(session, ledger) -> None:
    await ledger.normalize(*NO_CURRENCY, *TRADES)
    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            open_row(),
            close_row() | {"Details": f"{SYMBOL}/GBP"},
        ],
        [closed_row()],
    )
    report = await ledger.upload(body)
    assert f"statement currencies disagree for {SYMBOL}" in report.flagged
    assert not any("GBP" in flag or "USD" in flag for flag in report.flagged)
    # The first stated (the open's) is kept.
    [quote] = await _rows(
        session,
        "select q.symbol from finance.instruments i"
        " join finance.instruments q on q.id = i.quote_instrument_id"
        " join finance.instrument_identifiers ii on ii.instrument_id = i.id"
        " where ii.scheme = 'etoro_id' and ii.value = :v",
        v=str(INSTRUMENT_ID),
    )
    assert quote.symbol == "USD"


async def test_two_positions_naming_different_currencies_flagged(session, ledger) -> None:
    other = POSITION + 1
    second = api_trade(
        positionId=other,
        orderId=other + 500,
        openTimestamp="2026-10-03T14:30:05.000Z",
        closeTimestamp="2026-10-21T15:01:44.000Z",
    )
    await ledger.normalize(
        *NO_CURRENCY,
        *TRADES,
        ("trade_history", second),
        ("order_lookup", lookup_body(other + 500, other)),
    )

    def position(cells: dict[str, str], at: str, currency: str) -> dict[str, str]:
        return cells | {"Date": at, "Position ID": str(other), "Details": f"{SYMBOL}/{currency}"}

    body = build_statement(
        summary(username=USERNAME),
        [
            transfer_in(),
            open_row(),
            position(open_row(), "03/10/2026 14:30:05", "GBP"),
            close_row(),
            position(close_row(), "21/10/2026 15:01:44", "GBP"),
        ],
        [
            closed_row(),
            closed_row(str(other))
            | {"Open Date": "03/10/2026 14:30:05", "Close Date": "21/10/2026 15:01:44"},
        ],
    )
    report = await ledger.upload(body)
    assert f"statement currencies disagree for {SYMBOL}" in report.flagged
    found = await _ledger(session, ledger)
    assert f"close:{POSITION}" in found and f"close:{other}" in found


async def test_review_stays_open_when_a_source_stops_stating_its_field(session, ledger) -> None:
    await ledger.normalize(*BASE, *TRADES)
    await ledger.upload(trade_statement(closed_profit="25.00"))
    assert await _review_links(session, ledger) == [
        (f"close:{POSITION}", "profit", "20", "25", None, f"close:{POSITION}")
    ]
    # A re-issue states no profit (Closed Positions and the close row leave it out): nothing
    # agrees on it, so its review stays open.
    await ledger.upload(trade_statement(closed_profit="-", close_change="-"))
    [review] = await list_reviews(session, ledger.connection.id)
    assert (review.field, review.resolved_at) == ("profit", None)


async def test_a_placeholder_cash_is_flagged_not_zero(session, ledger) -> None:
    await ledger.normalize(*BASE)
    body = build_statement(
        summary(username=USERNAME),
        [transfer_in(), activity_row("05/10/2026 09:00:00", "Withdraw Fee", "-5.00", change="-")],
    )
    report = await ledger.upload(body)
    assert f"{ACCOUNT_ACTIVITY}.Realized Equity Change has no number" in report.flagged
    assert f"activity:{_key(body, 'Withdraw Fee')}" not in await _ledger(session, ledger)


async def test_a_malformed_period_is_a_data_error(session, ledger) -> None:
    await ledger.normalize(*BASE)
    r = core_models.RawRecord
    raw = r(
        connection_id=ledger.connection.id,
        sync_id=uuid.uuid4(),
        record_type="etoro.statement.account_activity",
        request={"period": {"from": "not a moment", "to": "2026-10-31T23:59:59+00:00"}},
        content={"cells": transfer_in(), "key": "0" * 32},
        content_hash="f" * 64,
        connector_version="etoro-sync-1",
        fetched_at=NOW,
    )
    session.add(raw)
    await session.flush()
    with pytest.raises(EtoroDataError, match="statement period"):
        await normalize(await ledger.context(), [raw.id])


async def test_isin_another_listing_holds_is_flagged_not_failed(session, run, ledger) -> None:
    usd, gbp = await get_currency(session, "USD"), await get_currency(session, "GBP")
    # One holder in another currency; then one in USD that already has an eToro id.
    await run(
        lambda ctx: create_instrument(
            ctx,
            kind="equity",
            symbol="ZZQA.L",
            name="Synthetic Quality A",
            decimals=4,
            quote_instrument_id=gbp.id,
            identifiers=[InstrumentIdentifier("isin", ISIN)],
        )
    )
    await ledger.normalize(*BASE, *TRADES)
    report = await ledger.upload(trade_statement())
    assert statement_ledger.ISIN_ELSEWHERE in report.flagged
    await session.execute(
        text("delete from finance.instrument_identifiers where value = :v"), {"v": ISIN}
    )
    await run(
        lambda ctx: create_instrument(
            ctx,
            kind="equity",
            symbol="ZZQA.X",
            name="Synthetic Quality A",
            decimals=4,
            quote_instrument_id=usd.id,
            identifiers=[
                InstrumentIdentifier("isin", ISIN),
                InstrumentIdentifier("etoro_id", "9999"),
            ],
        )
    )
    report = await ledger.upload(trade_statement(closed_units="4", closed_profit="20.0"))
    assert statement_ledger.NOT_MERGED in report.flagged
    # The upload went through; the trade stays on eToro's instrument.
    assert (await _postings(session, f"open:{POSITION}"))[0][1] == SYMBOL


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


# R39: an open is compared only when both sources describe the same part ----------------------


def _partial_statement(*, closed: bool, closed_units: str = "4") -> bytes:
    """Ten units opened; with `closed`, four of them closed (Closed Positions has them)."""
    open_ten = open_row() | {"Amount": "1000", "Units / Contracts": "10", "Balance": "260.00"}
    if not closed:
        return build_statement(summary(username=USERNAME), [transfer_in(), open_ten])
    return build_statement(
        summary(username=USERNAME),
        [transfer_in(), open_ten, close_row(balance="680.00") | {"Amount": "420.00"}],
        [closed_row() | {"Amount": "400", "Units / Contracts": closed_units}],
    )


API_OPENED = "2026-10-02T14:30:05.120Z"
PARTIAL = api_trade(investment=Decimal("400.00"), initialInvestment=Decimal("1000.00"))


async def test_a_statement_behind_a_partial_close_opens_no_review(session, ledger) -> None:
    # The API has the partial close (trade history: the four units closed); the statement
    # ends before it (the whole position's "Open Position" row): different facts.
    await ledger.normalize(
        *BASE, ("trade_history", PARTIAL), ("order_lookup", lookup_body(ORDER, POSITION))
    )
    before = await _ledger(session, ledger)
    await ledger.upload(_partial_statement(closed=False))
    assert await list_reviews(session, ledger.connection.id) == []
    assert ledger.disagreement_messages() == []
    # The API's postings stand; the statement adds only its transfer.
    found = await _ledger(session, ledger)
    assert {k: v for k, v in found.items() if not k.startswith("activity:")} == before
    # The next statement holds the close: like for like, and they agree.
    await ledger.upload(_partial_statement(closed=True))
    assert await list_reviews(session, ledger.connection.id) == []


async def test_a_statement_ahead_of_a_partial_close_opens_no_review(session, ledger) -> None:
    # The API's latest PnL still lists the whole position; the statement already has the
    # four units closed. Compared once trade history brings the close.
    await ledger.normalize(
        *BASE,
        (
            "pnl",
            pnl_body([pnl_position(POSITION, units="10", amount="1000.00", opened=API_OPENED)]),
        ),
        ("order_lookup", lookup_body(ORDER, POSITION)),
    )
    await ledger.upload(_partial_statement(closed=True))
    assert await list_reviews(session, ledger.connection.id) == []
    assert ledger.disagreement_messages() == []
    await ledger.normalize(*BASE, ("trade_history", PARTIAL), now=NOW + timedelta(days=1))
    assert await list_reviews(session, ledger.connection.id) == []


async def test_a_partial_close_the_sources_disagree_on_is_reviewed(session, ledger) -> None:
    # Both describe the part closed, and differ: a review, told once.
    await ledger.normalize(
        *BASE, ("trade_history", PARTIAL), ("order_lookup", lookup_body(ORDER, POSITION))
    )
    await ledger.upload(_partial_statement(closed=True, closed_units="5"))
    [review] = await list_reviews(session, ledger.connection.id)
    assert (review.event_key, review.field, review.api_value, review.statement_value) == (
        f"open:{POSITION}",
        "units",
        "4",
        "5",
    )
    assert len(ledger.disagreement_messages()) == 1


# R40: the unplaceable-position flag lives only as long as its cause ---------------------------

UNPLACED_FLAG = f"eToro flagged: {statement_ledger.UNPLACED}"
OPEN_ONLY = [transfer_in(), open_row()]


async def _warnings(session, ledger: Ledger) -> list[str]:
    return (await get_connection(session, ledger.connection.id)).warnings


async def test_a_statement_open_after_the_last_sync_is_not_flagged(session, ledger) -> None:
    # The last sync fetched before the position opened: the API is expected to bring it.
    await ledger.normalize(*BASE, now=datetime(2026, 10, 2, 9, 0, tzinfo=UTC))
    report = await ledger.upload(build_statement(summary(username=USERNAME), OPEN_ONLY))
    assert statement_ledger.UNPLACED not in report.flagged | report.flags_in_full
    assert UNPLACED_FLAG not in await _warnings(session, ledger)


async def test_the_unplaceable_flag_stands_then_clears_once_placed(session, ledger) -> None:
    # A sync after the open did not bring the position: the flag stands.
    await ledger.normalize(*BASE, now=NOW)
    await ledger.upload(build_statement(summary(username=USERNAME), OPEN_ONLY))
    assert UNPLACED_FLAG in await _warnings(session, ledger)
    # The next sync's PnL lists it: placed, and the flag clears without a replay.
    pnl = ("pnl", pnl_body([pnl_position(POSITION, units="4", amount="500.00")]))
    report = await ledger.normalize(*BASE, pnl, now=NOW + timedelta(days=1))
    assert not report.replayed
    assert statement_ledger.UNPLACED not in report.flagged | report.flags_in_full
    # The next upload publishes the flags in full: it is gone.
    await ledger.upload(build_statement(summary(username=USERNAME), OPEN_ONLY))
    assert UNPLACED_FLAG not in await _warnings(session, ledger)


async def _open_units(session) -> Decimal:
    return (await _postings(session, f"open:{POSITION}"))[0][2]


async def test_a_decision_holds_through_a_full_close(session, run, ledger) -> None:
    # PnL says two units, the statement's "Open Position" row three: the owner takes three.
    pnl = ("pnl", pnl_body([pnl_position(POSITION, units="2", amount="500.00", opened=API_OPENED)]))
    await ledger.normalize(*BASE, pnl, ("order_lookup", lookup_body(ORDER, POSITION)))
    three = open_row(units="3")
    await ledger.upload(build_statement(summary(username=USERNAME), [transfer_in(), three]))
    [review] = await list_reviews(session, ledger.connection.id)
    assert (review.field, review.api_value, review.statement_value) == ("units", "2", "3")
    await run(lambda ctx: resolve_review(ctx, review.id, "statement"))
    await ledger.normalize(*BASE, pnl, now=NOW + timedelta(days=1))
    assert await _open_units(session) == 3
    # It closes in full: trade history describes the whole position, like the row.
    whole = api_trade(units=Decimal(2))
    await ledger.normalize(*BASE, ("trade_history", whole), now=NOW + timedelta(days=2))
    assert await _open_units(session) == 3
    # A statement with its Closed Positions row: still three, nothing new to review.
    await ledger.upload(
        build_statement(
            summary(username=USERNAME),
            [transfer_in(), three, close_row() | {"Units / Contracts": "3"}],
            [closed_row() | {"Units / Contracts": "3"}],
        )
    )
    assert await _open_units(session) == 3
    reviews = await list_reviews(session, ledger.connection.id)
    # The open's item stays decided; the close, three units against two, is a new fact.
    assert sorted((r.event_key, r.field, r.resolution) for r in reviews) == [
        (f"close:{POSITION}", "units", None),
        (f"open:{POSITION}", "units", "statement"),
    ]
    assert len(ledger.disagreement_messages()) == 2


async def test_a_full_close_disagreeing_with_the_open_row_is_reviewed(session, ledger) -> None:
    # Trade history closed the whole position (four units); the statement, still before the
    # close, opened five: the same fact, stated differently.
    await ledger.normalize(
        *BASE, ("trade_history", api_trade()), ("order_lookup", lookup_body(ORDER, POSITION))
    )
    await ledger.upload(
        build_statement(summary(username=USERNAME), [transfer_in(), open_row(units="5")])
    )
    [review] = await list_reviews(session, ledger.connection.id)
    assert (review.field, review.api_value, review.statement_value) == ("units", "4", "5")
    assert len(ledger.disagreement_messages()) == 1
