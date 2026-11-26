"""Trading 212's CSV export, uploaded alongside the API (design §3a, §5.3a): optional, read
with the API's precedence (§5.7), and nothing depends on it."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from lararium.core.connections import get_connection
from lararium.modules.finance.source_reviews import list_reviews, resolve_review
from lararium.modules.finance.trading212.export import (
    MAX_CELL,
    MAX_ROWS,
    ExportError,
    ExportTooEarly,
    ingest_export,
    parse_export,
)

from .fixtures import order_item, position_json, summary_json, transaction_item
from .test_normalize import (
    ACCOUNT,
    META,
    SUMMARY,
    Ledger,
    Notifier,
    _api,
    _connection,
    _postings,
    _run_sync,
    _transaction,
)
from .test_normalize import _delete_derived as _delete_api_derived
from .test_normalize import _sync_connection as _synced_connection

HEADER = ("Action", "Time (UTC)", "Notes", "ID", "Total", "Currency (Total)")
REF = "00000000-0000-4000-8000-000000000001"
KEY = f"transaction:{REF}"
CARD_TX = "card-tx-0000-0001"


def row(
    reference: str = REF,
    action: str = "Deposit",
    total: str = "250.00",
    time: str = "2026-03-01 09:15:00+00:00",
    notes: str = f"Transaction ID: {CARD_TX}",
    currency: str = "GBP",
    **extra: str,
) -> dict[str, str]:
    return {
        "Action": action,
        "Time (UTC)": time,
        "Notes": notes,
        "ID": reference,
        "Total": total,
        "Currency (Total)": currency,
        **extra,
    }


def export_csv(*rows: dict[str, str], header: tuple[str, ...] = HEADER, bom: bool = False) -> bytes:
    """The export as Trading 212 writes it: a header, then one line per row, quoted as needed."""

    def line(cells: list[str]) -> str:
        return ",".join(f'"{c}"' if any(x in c for x in ',"\n') else c for c in cells)

    lines = [line(list(header))] + [line([r.get(h, "") for h in header]) for r in rows]
    body = "\r\n".join(lines) + "\r\n"
    return (b"\xef\xbb\xbf" if bom else b"") + body.encode()


async def _ingest(ledger: Ledger, body: bytes, filename: str = "export.csv"):
    return await ingest_export(await ledger.context(), body, filename)


async def _export_row(session, reference: str = REF):
    return (
        await session.execute(
            text("select * from finance.trading212_export_rows where id = :i"), {"i": reference}
        )
    ).one_or_none()


async def _warnings(session, connection) -> list[str]:
    return (await get_connection(session, connection.id)).warnings


# Reading the file ------------------------------------------------------------------------


def test_parse_export_columns() -> None:
    rows, flagged = parse_export(export_csv(row()))
    [only] = rows
    assert only.key == REF
    assert dict(only.cells) == row()
    assert flagged == frozenset()

    # A byte-order mark is not part of the first header.
    assert parse_export(export_csv(row(), bom=True)) == (rows, flagged)

    # A required column missing is named.
    without_id = tuple(h for h in HEADER if h != "ID")
    with pytest.raises(ExportError, match=r"^the export lacks its ID column$"):
        parse_export(export_csv(row(), header=without_id))

    # A column not known, with a value, is flagged by its header and kept; one without a value
    # in any row is not flagged.
    header = (*HEADER, "Ticker", "Empty column")
    rows, flagged = parse_export(export_csv(row(Ticker="ZZQA"), header=header))
    assert flagged == frozenset({"Ticker"})
    assert rows[0].cells["Ticker"] == "ZZQA"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"Action,ID\xff\r\n", "the export is not UTF-8 text"),
        (export_csv(row(total="lots")), "the export's Total is not a number"),
        (export_csv(row(time="2026-03-01T09:15:00Z")), "the export's Time (UTC) is not"),
        (export_csv(row(reference="")), "a row of the export lacks its ID"),
        (export_csv(row(), row(total="1.00")), "the export has two different rows with one ID"),
        (export_csv(row(currency="pounds")), "the export's Currency (Total) is not a currency"),
        (b'Action,ID\r\n"unterminated\x00,1\r\n', "the export is not a readable CSV file"),
    ],
)
def test_a_bad_file_is_refused_naming_no_value(body: bytes, message: str) -> None:
    with pytest.raises(ExportError) as refused:
        parse_export(body)
    assert str(refused.value).startswith(message)
    for value in ("lots", "pounds", "unterminated", CARD_TX):
        assert value not in str(refused.value)


def test_quoted_commas_and_newlines_stay_in_their_cell() -> None:
    notes = 'Card, "main"\nsecond line'
    body = (
        b"Action,Time (UTC),Notes,ID,Total,Currency (Total)\r\n"
        b'Deposit,2026-03-01 09:15:00+00:00,"Card, ""main""\nsecond line",ref-q,250.00,GBP\r\n'
    )
    [only], flagged = parse_export(body)
    assert (only.key, only.cells["Notes"], only.cells["Total"]) == ("ref-q", notes, "250.00")
    assert flagged == frozenset()


@pytest.mark.parametrize(
    ("spelt", "named"),
    [("action", "Action"), ("Time(UTC)", "Time (UTC)"), ("Currency  (Total)", "Currency (Total)")],
)
def test_a_header_spelt_otherwise_is_refused(spelt: str, named: str) -> None:
    header = tuple(spelt if h == named else h for h in HEADER)
    with pytest.raises(ExportError) as refused:
        parse_export(export_csv(row(), header=header))
    assert str(refused.value) == f"the export lacks its {named} column"


def test_a_file_without_a_header_names_no_cell() -> None:
    # Its first data row stands where the header should, two of its cells equal.
    body = b"Deposit,2026-03-01 09:15:00+00:00,250.00,ref-h,250.00,GBP\r\n"
    with pytest.raises(ExportError) as refused:
        parse_export(body)
    assert str(refused.value) == "the export lacks its Action column"
    for value in ("250.00", "ref-h", "Deposit"):
        assert value not in str(refused.value)
    # A real header listing a column twice is refused without naming it.
    with pytest.raises(ExportError, match=r"^the export lists a column twice$"):
        parse_export(export_csv(row(), header=(*HEADER, "Notes")))


def test_rows_and_cells_are_capped() -> None:
    assert (MAX_ROWS, MAX_CELL) == (200_000, 32_767)
    long_note = row(notes="x" * (MAX_CELL + 1))
    with pytest.raises(ExportError, match="longer than 32767 characters"):
        parse_export(export_csv(long_note))
    many = export_csv(*(row(reference=f"r{n}") for n in range(MAX_ROWS + 1)))
    with pytest.raises(ExportError, match="more than 200000 rows"):
        parse_export(many)


def test_the_same_row_twice_is_one_row() -> None:
    rows, _ = parse_export(export_csv(row(), row()))
    assert len(rows) == 1


# Into the ledger -------------------------------------------------------------------------


async def test_notes_transaction_id_typed(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY)
    other = row(reference="ref-2", notes="Bank transfer")
    await _ingest(ledger, export_csv(row(currency=" GBP "), other))

    typed = await _export_row(session)
    assert (typed.notes, typed.notes_transaction_id) == (f"Transaction ID: {CARD_TX}", CARD_TX)
    assert (typed.action, typed.total, typed.currency_total) == (
        "Deposit",
        Decimal("250.00"),
        "GBP",
    )
    assert typed.time_utc.isoformat() == "2026-03-01T09:15:00+00:00"
    plain = await _export_row(session, "ref-2")
    assert (plain.notes, plain.notes_transaction_id) == ("Bank transfer", None)
    # Each row is a raw record of its cells, from the export's document.
    record = (
        await session.execute(
            text(
                "select content, request from core.raw_records"
                " where record_type = 'trading212.export_row' and content->'cells'->>'ID' = :i"
            ),
            {"i": REF},
        )
    ).one()
    assert record.content == {"cells": row(currency=" GBP ")}
    document = (
        await session.execute(text("select source, original_filename, sha256 from core.documents"))
    ).one()
    assert (document.source, document.original_filename) == ("finance.trading212", "export.csv")
    assert record.request["document_sha256"] == document.sha256


async def test_export_row_enriches_api_deposit(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, ("transactions", transaction_item()))
    before = await _postings(session, KEY)
    posted = await _transaction(session, KEY)

    await _ingest(ledger, export_csv(row()))

    assert await _postings(session, KEY) == before
    assert await _transaction(session, KEY) == posted
    typed = await _export_row(session)
    assert typed.transaction_id == posted.id
    assert await list_reviews(session, connection.id) == []
    assert await _warnings(session, connection) == []


async def test_export_only_deposit_posts_then_api_enriches(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    api_first, export_first = "ref-api-first", "ref-export-first"
    # One deposit arrives from the API first, the other from the export first.
    await ledger.normalize(SUMMARY, ("transactions", transaction_item(reference=api_first)))
    await _ingest(ledger, export_csv(row(reference=api_first), row(reference=export_first)))

    # The export alone posts the deposit it has and the API lacks, under the API's key.
    alone = await _transaction(session, f"transaction:{export_first}")
    assert (alone.description, alone.bank_transfer) == ("Deposit", False)
    assert await _postings(session, f"transaction:{export_first}") == [
        (ACCOUNT, "GBP", Decimal("250.00"), None, None, None, None, False, ""),
        ("Unassigned", "GBP", Decimal("-250.00"), None, None, None, None, False, ""),
    ]

    report = await ledger.normalize(
        SUMMARY, ("transactions", transaction_item(reference=export_first))
    )
    # The API's arrival only enriches it: its values are the same, so nothing is re-posted.
    assert report.updated == 0
    assert await _transaction(session, f"transaction:{export_first}") == alone

    async def state(reference: str) -> tuple[Any, ...]:
        posted = await _transaction(session, f"transaction:{reference}")
        typed = await _export_row(session, reference)
        return (
            posted.occurred_at,
            posted.booking_date,
            posted.description,
            posted.bank_transfer,
            posted.status,
            await _postings(session, f"transaction:{reference}"),
            typed.transaction_id == posted.id,
        )

    assert await state(api_first) == await state(export_first)
    assert await list_reviews(session, connection.id) == []


async def test_export_withdrawal_signs_by_action(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY)
    await _ingest(ledger, export_csv(row(reference="w-1", action="Withdrawal", total="-40.00")))
    [own, contra] = await _postings(session, "transaction:w-1")
    assert (own[0], own[2], contra[0], contra[2]) == (
        ACCOUNT,
        Decimal("-40.00"),
        "Unassigned",
        Decimal("40.00"),
    )
    assert (await _transaction(session, "transaction:w-1")).description == "Withdrawal"


async def test_trade_row_flagged_not_posted(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY)
    header = (*HEADER, "Ticker", "No. of shares")
    trade = row(reference="EOF000001", action="Market buy", total="-241.20", notes="")
    trade |= {"Ticker": "ZZQA", "No. of shares": "3"}

    await _ingest(ledger, export_csv(trade, header=header))

    assert await _transaction(session, "transaction:EOF000001") is None
    typed = await _export_row(session, "EOF000001")
    assert (typed.action, typed.total, typed.transaction_id) == (
        "Market buy",
        Decimal("-241.20"),
        None,
    )
    assert sorted(await _warnings(session, connection)) == [
        "Trading 212 flagged: export action Market buy",
        "Trading 212 flagged: export column No. of shares",
        "Trading 212 flagged: export column Ticker",
    ]
    # The owner is told once, and the values stay in the raw record only.
    assert len(ledger.notifier.messages) == 1
    assert "ZZQA" not in ledger.notifier.messages[0]
    stored = await session.scalar(
        text(
            "select content from core.raw_records where record_type = 'trading212.export_row'"
            " and content->'cells'->>'ID' = 'EOF000001'"
        )
    )
    assert stored["cells"]["Ticker"] == "ZZQA"


async def test_an_unknown_column_unfit_to_show_is_counted_not_named(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY)
    header = (*HEADER, "Ticker", "<b>Odd</b>", "x" * 41)
    deposit = row() | {"Ticker": "ZZQA", "<b>Odd</b>": "1", "x" * 41: "2"}

    await _ingest(ledger, export_csv(deposit, header=header))

    assert sorted(w for w in await _warnings(session, connection) if "export column" in w) == [
        "Trading 212 flagged: export column <unknown column> (2)",
        "Trading 212 flagged: export column Ticker",
    ]


async def test_upload_twice_writes_nothing(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, ("transactions", transaction_item()))
    body = export_csv(row(), row(reference="ref-2"))
    first = await _ingest(ledger, body)

    second = await _ingest(ledger, body)

    assert sorted(second.raw_record_ids) == sorted(first.raw_record_ids)
    assert second.document_sha256 == first.document_sha256
    assert (ledger.events(), ledger.changes()) == (0, 0)


async def test_an_export_before_the_first_sync_is_too_early(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    with pytest.raises(ExportTooEarly, match="first sync"):
        await _ingest(ledger, export_csv(row()))
    assert await session.scalar(text("select count(*) from core.documents")) == 0


async def test_other_accounts_ids_refused(session, run) -> None:
    mine = await _connection(run)
    theirs = await _connection(run, "isa")
    other = Ledger(session, theirs)
    await other.normalize(
        ("summary", summary_json(account_id=20000002)), ("transactions", transaction_item())
    )
    ledger = Ledger(session, mine)
    await ledger.normalize(SUMMARY)

    with pytest.raises(ExportError) as refused:
        await _ingest(ledger, export_csv(row()))

    assert str(refused.value) == "this export is of another Trading 212 account"
    assert REF not in str(refused.value)
    assert await _export_row(session) is None
    # An export whose IDs no account's records know is accepted: it may predate the API.
    await _ingest(ledger, export_csv(row(reference="ref-unknown")))


async def test_a_reference_both_accounts_hold_is_accepted(session, run) -> None:
    # A transfer between the owner's Invest and ISA accounts may share its reference.
    mine = await _connection(run)
    theirs = await _connection(run, "isa")
    shared = transaction_item(reference="shared-1", kind="TRANSFER", amount="-20.00")
    # The ISA's side of the same transfer, under the same reference.
    await Ledger(session, theirs).normalize(
        ("summary", summary_json(account_id=20000002)),
        ("transactions", transaction_item(reference="shared-1", kind="TRANSFER")),
    )
    ledger = Ledger(session, mine)
    await ledger.normalize(SUMMARY, ("transactions", shared))

    await _ingest(ledger, export_csv(row(reference="shared-1", action="Transfer", total="-20")))

    linked = await session.scalar(
        text(
            "select t.connection_id from finance.trading212_export_rows e"
            " join finance.transactions t on t.id = e.transaction_id where e.id = 'shared-1'"
        )
    )
    assert linked == mine.id


# Precedence and reviews (design §5.7) -------------------------------------------------------


async def _disagreeing(session, run, **overrides: str) -> tuple[Any, Ledger]:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, ("transactions", transaction_item()))
    await _ingest(ledger, export_csv(row(**overrides)))
    return connection, ledger


async def test_a_disagreement_opens_a_review_and_the_api_posts(session, run) -> None:
    connection, ledger = await _disagreeing(session, run, total="260.00")
    [review] = await list_reviews(session, connection.id)
    assert (review.event_key, review.field) == (KEY, "amount")
    assert (review.api_value, review.statement_value) == ("250", "260")
    assert (await _postings(session, KEY))[0][2] == Decimal("250.00")
    posted = await _transaction(session, KEY)
    assert review.transaction_id == posted.id
    # The owner is told, naming the transaction by its last four characters, no value.
    [told] = [m for m in ledger.notifier.messages if "disagree" in m]
    assert "260" not in told and "250" not in told and REF not in told
    assert "transaction ···0001 (amount)" in told


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


async def test_a_failed_review_notification_is_sent_next_run(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, ("transactions", transaction_item()))
    ledger.notifier = _FailingOnce(ledger.notifier.messages)
    await _ingest(ledger, export_csv(row(total="260.00")))
    assert [m for m in ledger.notifier.messages if "disagree" in m] == []

    await ledger.normalize(SUMMARY)
    [told] = [m for m in ledger.notifier.messages if "disagree" in m]
    assert "transaction ···0001 (amount)" in told and "resolved through the API" in told
    await ledger.normalize(SUMMARY)
    assert len([m for m in ledger.notifier.messages if "disagree" in m]) == 1


async def test_taking_the_export_posts_its_value_and_replays(session, run) -> None:
    connection, ledger = await _disagreeing(
        session, run, total="260.00", time="2026-03-02 00:30:00+00:00"
    )
    reviews = {r.field: r for r in await list_reviews(session, connection.id)}
    assert set(reviews) == {"amount", "booking_date"}
    for review in reviews.values():
        await run(resolve_review, review.id, "statement")
    await ledger.replay()

    posted = await _transaction(session, KEY)
    assert posted.booking_date.isoformat() == "2026-03-02"
    assert (await _postings(session, KEY))[0][2] == Decimal("260.00")
    # Regeneration replays the decision.
    await _delete_derived(session, connection)
    await ledger.replay()
    assert (await _postings(session, KEY))[0][2] == Decimal("260.00")
    assert (await _transaction(session, KEY)).booking_date.isoformat() == "2026-03-02"


async def test_agreeing_again_closes_the_review(session, run) -> None:
    connection, ledger = await _disagreeing(session, run, total="260.00")
    # A later export agrees with the API.
    await _ingest(ledger, export_csv(row()), "export-2.csv")
    await ledger.replay()
    [review] = await list_reviews(session, connection.id)
    assert (review.resolution, review.resolved_by) == ("api", "sources agree")


async def test_a_currency_disagreement_is_reviewed(session, run) -> None:
    connection, _ = await _disagreeing(session, run, currency="EUR")
    [review] = await list_reviews(session, connection.id)
    assert (review.field, review.api_value, review.statement_value) == ("currency", "GBP", "EUR")


# Regeneration and optionality -------------------------------------------------------------


async def _delete_derived(session, connection) -> None:
    await _delete_api_derived(session, connection)
    await session.execute(
        text("delete from finance.trading212_export_rows where connection_id = :c"),
        {"c": connection.id},
    )


async def test_regenerate_with_an_export(session, run) -> None:
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, META, ("transactions", transaction_item()))
    header = (*HEADER, "Ticker")
    await _ingest(
        ledger,
        export_csv(
            row(),
            row(reference="ref-only", total="12.00"),
            row(reference="EOF1", action="Market buy", Ticker="ZZQA"),
            header=header,
        ),
    )

    async def state() -> list[tuple[Any, ...]]:
        rows = await session.execute(
            text(
                "select t.external_id, t.status, t.occurred_at, t.description,"
                " (select json_agg(json_build_array(p.account_id, p.quantity::text)"
                "  order by p.position) from finance.postings p"
                "  where p.transaction_id = t.id) as postings,"
                " (select to_jsonb(e) - 'transaction_id' - 'raw_record_id'"
                "  from finance.trading212_export_rows e where e.transaction_id = t.id) as row"
                " from finance.transactions t where t.connection_id = :c"
                " order by t.external_id"
            ),
            {"c": connection.id},
        )
        unlinked = await session.execute(
            text(
                "select to_jsonb(e) - 'raw_record_id' as row from finance.trading212_export_rows e"
                " where e.connection_id = :c and e.transaction_id is null order by e.id"
            ),
            {"c": connection.id},
        )
        return [tuple(r) for r in rows] + [tuple(r) for r in unlinked]

    before = await state()
    assert len(before) == 3 + 1  # the opening, two deposits, and the trade row
    await _delete_derived(session, connection)
    await ledger.replay()
    assert await state() == before


async def test_an_upload_takes_the_stored_row_as_a_replay_does(session, run) -> None:
    """One ID uploaded with two contents, then the first again (stored once, so it keeps its
    first fetch): the upload resolves the row as the replay does, the latest stored copy."""
    connection = await _connection(run)
    ledger = Ledger(session, connection)
    await ledger.normalize(SUMMARY, META)
    first = export_csv(row(reference="ref-twice", total="12.00"))
    await _ingest(ledger, first)
    await _ingest(ledger, export_csv(row(reference="ref-twice", total="13.00")))
    await _ingest(ledger, first)
    uploaded = await _postings(session, "transaction:ref-twice")

    await _delete_derived(session, connection)
    await ledger.replay()

    assert await _postings(session, "transaction:ref-twice") == uploaded


async def test_works_without_export(session, run, monkeypatch) -> None:
    api = _api(monkeypatch)
    # One account's whole history: a deposit and a buy that explain its report.
    api.summary = summary_json()
    api.positions = [position_json()]
    api.history = {
        "orders": [[order_item()]],
        "dividends": [[]],
        "transactions": [[transaction_item(amount="491.95")]],
    }
    api.pies = []
    connection = await _synced_connection(run)
    notifier = Notifier()

    after = await _run_sync(session, connection, api, notifier)

    assert after.status == "healthy"
    assert after.warnings == []
    assert not [w for w in after.warnings if "export" in w.lower()]
    flags = after.cursor.get("flags", {}) if isinstance(after.cursor, dict) else {}
    assert not [f for reasons in flags.values() for f in reasons if "export" in f]
    deposit = await _transaction(session, KEY)
    assert deposit is not None and deposit.status != "void"
    # Reconciled clean, with no opening: the API alone made the books.
    assert after.cursor.get("reconciliation") == {}
    opening = await session.scalar(
        text(
            "select count(*) from finance.transactions"
            " where connection_id = :c and external_id like 'opening:%' and status <> 'void'"
        ),
        {"c": connection.id},
    )
    assert opening == 0
    exported = await session.scalar(text("select count(*) from finance.trading212_export_rows"))
    assert exported == 0
    assert not [m for m in notifier.messages if "export" in m.lower()]
