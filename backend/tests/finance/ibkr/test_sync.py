import base64
import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from functools import partial

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.config import Settings
from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    set_status,
    store_credential,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.core.documents import LocalDocumentStore
from lararium.core.json_types import JSONValue
from lararium.modules.finance.ibkr import sync as ibkr_sync
from lararium.modules.finance.ibkr.client import FlexClient
from lararium.modules.finance.ibkr.parser import FlexDataError, parse_statement
from lararium.modules.finance.ibkr.sync import (
    LONDON,
    IbkrConnector,
    _opened,
    due,
    ingest_statement,
    windows,
)
from lararium.worker import jobs

from .fixtures import AccountSpec, RowSpec, build_statement

KEY = bytes(range(32))
TOKEN = "tok-9f8e7d6c5b4a"
CONNECTOR = Actor("connector", "ibkr")
ACCOUNT = AccountSpec("U0000001")
SETTINGS = Settings(
    database_url="postgresql+psycopg://x@localhost/x",
    auth_mode="dev",
    master_key=SecretStr(base64.b64encode(KEY).decode()),
)


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url.path}")


@pytest.mark.parametrize(
    ("fetched_at", "now", "expected"),
    [
        # BST: 06:00 London is 05:00 UTC.
        ("2026-07-01T04:30:00+00:00", "2026-07-01T05:30:00+00:00", True),
        ("2026-07-01T05:10:00+00:00", "2026-07-01T05:30:00+00:00", False),
        # Before 06:00 the boundary is the day before's.
        ("2026-06-30T05:10:00+00:00", "2026-07-01T04:59:00+00:00", False),
        ("2026-06-30T04:30:00+00:00", "2026-07-01T04:59:00+00:00", True),
        # GMT: 06:00 London is 06:00 UTC.
        ("2026-01-10T05:30:00+00:00", "2026-01-10T06:30:00+00:00", True),
        ("2026-01-10T06:10:00+00:00", "2026-01-10T06:30:00+00:00", False),
        ("2026-01-09T06:10:00+00:00", "2026-01-10T05:59:00+00:00", False),
    ],
)
def test_due_after_six_london(fetched_at: str, now: str, expected: bool) -> None:
    cursor: JSONValue = {"fetched_at": fetched_at}
    assert due(cursor, datetime.fromisoformat(now)) is expected


@pytest.mark.parametrize("cursor", [None, {}, {"fetched_to": "2026-11-10"}])
def test_due_without_a_fetch_time(cursor: JSONValue) -> None:
    assert due(cursor, datetime(2026, 11, 18, 12, tzinfo=UTC)) is True


def test_windows_backfill_and_overlap() -> None:
    today = date(2026, 11, 18)
    assert windows(None, today, None) == [(date(2025, 11, 18), date(2026, 11, 17))]
    assert windows(None, today, date(2024, 3, 1)) == [
        (date(2024, 3, 1), date(2024, 11, 17)),
        (date(2024, 11, 18), date(2025, 11, 17)),
        (date(2025, 11, 18), date(2026, 11, 17)),
    ]
    # An account opened long ago is cut off at 1 January four years back.
    assert windows(None, today, date(2001, 1, 1))[0] == (date(2022, 1, 1), date(2022, 11, 18))
    # A later sync re-fetches the last 7 days.
    cursor: JSONValue = {"fetched_to": "2026-11-10"}
    assert windows(cursor, today, None) == [(date(2026, 11, 4), date(2026, 11, 17))]
    # An outage of 500 days: split into pieces of at most 365 days, oldest first.
    cursor = {"fetched_to": (today - timedelta(days=500)).isoformat()}
    pieces = windows(cursor, today, None)
    assert len(pieces) == 2
    assert pieces[0][0] == today - timedelta(days=506)
    assert pieces[0][1] - pieces[0][0] == timedelta(days=364)
    assert pieces[1] == (pieces[0][1] + timedelta(days=1), date(2026, 11, 17))


async def _connection(
    run, *, cursor: JSONValue = None, expires_at: datetime | None = None
) -> ConnectionRecord:
    async def setup(ctx: CommandContext) -> ConnectionRecord:
        connection = await create_connection(
            ctx, connector="ibkr", display_name="IBKR", config={"query_id": "111111"}
        )
        await store_credential(ctx, KEY, connection.id, "flex_token", TOKEN, expires_at)
        await ctx.session.execute(
            text(
                "update core.connections set status = 'healthy', cursor = cast(:c as jsonb),"
                " credential_expires_at = :e where id = :id"
            ),
            {"c": json.dumps(cursor), "e": expires_at, "id": connection.id},
        )
        return connection

    return await run(setup)


async def _context(session, connection, http, tmp_path, notifier=None) -> SyncContext:
    return SyncContext(
        esplora_url="https://esplora.test/api",
        command=CommandContext(session, CONNECTOR, uuid.uuid4()),
        connection=await get_connection(session, connection.id),
        sync_id=uuid.uuid4(),
        master_key=KEY,
        http=http,
        notifier=notifier or RecordingNotifier(),
        session_factory=async_sessionmaker(bind=session.bind),
        documents=LocalDocumentStore(tmp_path),
    )


def _statement(rows: list[RowSpec], period: tuple[date, date]) -> bytes:
    return build_statement([ACCOUNT], period=period, rows=rows, by_day=True)


async def test_ingest_stores_document_and_rows(session, run, tmp_path) -> None:
    connection = await _connection(run)
    day = date(2026, 11, 10)
    body = _statement(
        [
            RowSpec("CashTransaction", "U0000001", day=day, transactionID="8000001"),
            RowSpec("ConversionRate", "U0000001"),
        ],
        (day, day),
    )
    request: dict[str, JSONValue] = {"query_id": "111111", "fd": "2026-11-10", "td": "2026-11-10"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await _context(session, connection, http, tmp_path)
        result = await ingest_statement(ctx, body, request)

    docs = (await session.execute(text("select source, sha256 from core.documents"))).all()
    assert [d.source for d in docs] == ["finance.ibkr"]
    rows = (
        await session.execute(
            text(
                "select id, record_type, request, content from core.raw_records"
                " where connection_id = :c order by record_type"
            ),
            {"c": connection.id},
        )
    ).all()
    assert [r.record_type for r in rows] == [
        "ibkr.account_information",
        "ibkr.cash_transaction",
        "ibkr.conversion_rate",
    ]
    assert {r.id for r in rows} == set(result.raw_record_ids)
    assert result.flagged == frozenset()
    cash = next(r for r in rows if r.record_type == "ibkr.cash_transaction")
    assert cash.content["account"] == "U0000001"
    assert cash.content["period"] == {"from": "2026-11-10", "to": "2026-11-10"}
    assert cash.content["attributes"]["transactionID"] == "8000001"
    assert cash.request == {**request, "document_sha256": docs[0].sha256}


async def test_overlapping_rows_stored_once(session, run, tmp_path) -> None:
    connection = await _connection(run)
    shared = RowSpec("CashTransaction", "U0000001", day=date(2026, 11, 3), transactionID="8000002")
    first = _statement([shared], (date(2026, 11, 1), date(2026, 11, 3)))
    second = _statement([shared], (date(2026, 11, 3), date(2026, 11, 5)))
    request: dict[str, JSONValue] = {"upload": "x.xml"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await _context(session, connection, http, tmp_path)
        a = await ingest_statement(ctx, first, request)
        b = await ingest_statement(ctx, second, request)

    cash = text(
        "select id from core.raw_records where record_type = 'ibkr.cash_transaction'"
        " and content->'attributes'->>'transactionID' = '8000002'"
    )
    [only] = (await session.execute(cash)).scalars().all()
    assert only in a.raw_record_ids
    assert only in b.raw_record_ids


async def test_not_due_makes_no_request(session, run, tmp_path) -> None:
    cursor: JSONValue = {
        "history_from": "2025-11-18",
        "fetched_to": "2026-11-17",
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    connection = await _connection(run, cursor=cursor)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        ctx = await _context(session, connection, http, tmp_path)
        assert await IbkrConnector().sync(ctx) == cursor


async def test_expiry_reminded_once(session, run, tmp_path) -> None:
    expires_at = datetime.now(UTC) + timedelta(days=10)
    cursor: JSONValue = {"fetched_at": datetime.now(UTC).isoformat()}
    connection = await _connection(run, cursor=cursor, expires_at=expires_at)
    notifier = RecordingNotifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        for _ in range(3):
            ctx = await _context(session, connection, http, tmp_path, notifier)
            await IbkrConnector().sync(ctx)
        assert len(notifier.messages) == 1
        assert TOKEN not in notifier.messages[0]
        assert "https://" not in notifier.messages[0]

        # Task 8's update of the token clears the marker.
        await run(update_config, connection.id, remove=["expiry_notified_for"])
        ctx = await _context(session, connection, http, tmp_path, notifier)
        await IbkrConnector().sync(ctx)
        assert len(notifier.messages) == 2


async def test_a_far_expiry_is_not_reminded(session, run, tmp_path) -> None:
    expires_at = datetime.now(UTC) + timedelta(days=200)
    cursor: JSONValue = {"fetched_at": datetime.now(UTC).isoformat()}
    connection = await _connection(run, cursor=cursor, expires_at=expires_at)
    notifier = RecordingNotifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        await IbkrConnector().sync(await _context(session, connection, http, tmp_path, notifier))
    assert notifier.messages == []


@pytest.mark.parametrize("code", [1011, 1012, 1013, 1014, 1015, 1016])
async def test_token_rejected_sets_needs_reauth(session, run, monkeypatch, code: int) -> None:
    async def no_sleep(seconds: int) -> None:
        return None

    monkeypatch.setattr(ibkr_sync, "FlexClient", partial(FlexClient, sleep=no_sleep))
    connection = await _connection(run)
    notifier = RecordingNotifier()

    def expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                (
                    f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}</ErrorCode>"
                    "<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
                ).encode()
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(expired)) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=SETTINGS,
            http=http,
            notifier=notifier,
            session_factory=async_sessionmaker(bind=session.bind),
        )

    after = await get_connection(session, connection.id)
    assert after.status == "needs_reauth"
    assert len(notifier.messages) == 1
    assert TOKEN not in notifier.messages[0]
    assert TOKEN not in (after.last_error or "")


async def test_sync_fetches_every_window_and_returns_the_cursor(
    session, run, tmp_path, monkeypatch
) -> None:
    async def no_sleep(seconds: int) -> None:
        return None

    monkeypatch.setattr(ibkr_sync, "FlexClient", partial(FlexClient, sleep=no_sleep))
    connection = await _connection(run)
    today = datetime.now(UTC).date()
    opened = today - timedelta(days=400)
    account = AccountSpec("U0000001", date_opened=opened)
    asked: list[tuple[str, str]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path.endswith("SendRequest"):
            asked.append((params["fd"], params["td"]))
            return httpx.Response(
                200,
                content=b"<FlexStatementResponse><Status>Success</Status>"
                b"<ReferenceCode>1</ReferenceCode></FlexStatementResponse>",
            )
        fd = date.fromisoformat(asked[-1][0])
        return httpx.Response(
            200,
            content=build_statement([account], period=(fd, fd), rows=[], by_day=True),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        ctx = await _context(session, connection, http, tmp_path)
        cursor = await IbkrConnector().sync(ctx)

    assert isinstance(cursor, dict)
    assert len(asked) == 2
    assert cursor["fetched_to"] == date.fromisoformat(asked[0][1]).isoformat()
    assert cursor["history_from"] == min(date.fromisoformat(fd) for fd, _ in asked).isoformat()
    assert "fetched_at" in cursor
    await run(set_status, connection.id, "healthy")


def test_connector_identity() -> None:
    connector = IbkrConnector()
    assert (connector.name, connector.version) == ("ibkr", "ibkr-sync-1")
    assert connector.capabilities == {"balances", "holdings", "transactions"}


def test_a_bad_date_opened_is_named_without_its_value() -> None:
    body = build_statement(
        [ACCOUNT],
        period=(date(2026, 11, 10), date(2026, 11, 10)),
        rows=[RowSpec("AccountInformation", "U0000001", dateOpened="not-a-date")],
    )
    with pytest.raises(FlexDataError) as error:
        _opened(parse_statement(body))
    assert "AccountInformation.dateOpened" in str(error.value)
    assert "not-a-date" not in str(error.value)


async def test_a_failing_older_window_leaves_nothing(session, run, tmp_path, monkeypatch) -> None:
    async def no_sleep(seconds: int) -> None:
        return None

    monkeypatch.setattr(ibkr_sync, "FlexClient", partial(FlexClient, sleep=no_sleep))
    connection = await _connection(run)
    account = AccountSpec("U0000001", date_opened=datetime.now(UTC).date() - timedelta(days=400))
    sent: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("SendRequest"):
            sent.append(request.url.params["fd"])
            if len(sent) == 2:
                return httpx.Response(
                    200,
                    content=b"<FlexStatementResponse><Status>Fail</Status>"
                    b"<ErrorCode>1018</ErrorCode></FlexStatementResponse>",
                )
            return httpx.Response(
                200,
                content=b"<FlexStatementResponse><Status>Success</Status>"
                b"<ReferenceCode>1</ReferenceCode></FlexStatementResponse>",
            )
        fd = date.fromisoformat(sent[-1])
        return httpx.Response(
            200, content=build_statement([account], period=(fd, fd), rows=[], by_day=True)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=SETTINGS.model_copy(update={"document_dir": tmp_path}),
            http=http,
            notifier=RecordingNotifier(),
            session_factory=async_sessionmaker(bind=session.bind),
        )

    assert len(sent) == 2
    count = await session.scalar(
        text("select count(*) from core.raw_records where connection_id = :c"),
        {"c": connection.id},
    )
    assert count == 0
    after = await get_connection(session, connection.id)
    assert after.cursor is None
    assert after.status == "failing"


def _serving(statement_for):
    """A Flex Web Service whose SendRequest succeeds and whose GetStatement answers with
    statement_for(the window's first day)."""
    asked: list[date] = []

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("SendRequest"):
            asked.append(date.fromisoformat(request.url.params["fd"]))
            return httpx.Response(
                200,
                content=b"<FlexStatementResponse><Status>Success</Status>"
                b"<ReferenceCode>1</ReferenceCode></FlexStatementResponse>",
            )
        return httpx.Response(200, content=statement_for(asked[-1]))

    return serve


async def _run_sync(session, connection, serve, tmp_path, notifier) -> ConnectionRecord:
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        await jobs.run_sync(
            session,
            connection.id,
            settings=SETTINGS.model_copy(update={"document_dir": tmp_path}),
            http=http,
            notifier=notifier,
            session_factory=async_sessionmaker(bind=session.bind),
        )
    return await get_connection(session, connection.id)


@pytest.fixture
def no_sleep(monkeypatch) -> None:
    async def instantly(seconds: int) -> None:
        return None

    monkeypatch.setattr(ibkr_sync, "FlexClient", partial(FlexClient, sleep=instantly))


async def test_a_failed_sync_names_the_field(session, run, tmp_path, no_sleep) -> None:
    connection = await _connection(run)
    account = AccountSpec("U0000001", date_opened=datetime.now(UTC).date() - timedelta(days=30))
    serve = _serving(
        lambda fd: build_statement(
            [account], period=(fd, fd), rows=[RowSpec("Trade", "U0000001", reportDate=None)]
        )
    )
    notifier = RecordingNotifier()
    for _ in range(jobs.NOTIFY_AFTER_FAILURES):
        after = await _run_sync(session, connection, serve, tmp_path, notifier)
    expected = "Trade.reportDate is missing: select it in the Flex query"
    assert after.status == "failing"
    assert after.last_error == expected
    assert len(notifier.messages) == 1
    assert expected in notifier.messages[0]


async def test_a_failed_sync_shows_no_account_id_or_value(session, run, tmp_path, no_sleep) -> None:
    connection = await _connection(run)
    account = AccountSpec("U0000001", date_opened=datetime.now(UTC).date() - timedelta(days=30))
    rows = [
        RowSpec("Trade", "U0000001", transactionID="9000001", quantity="10"),
        RowSpec("Trade", "U0000001", transactionID="9000001", quantity="11"),
    ]
    serve = _serving(lambda fd: build_statement([account], period=(fd, fd), rows=rows))
    after = await _run_sync(session, connection, serve, tmp_path, RecordingNotifier())
    assert after.last_error == "Trade has two different rows for 9000001 of ···0001"
    assert "U0000001" not in after.last_error


async def test_a_response_without_statements_fails_and_keeps_the_cursor(
    session, run, tmp_path, no_sleep
) -> None:
    cursor: JSONValue = {
        "history_from": "2025-11-18",
        "fetched_to": "2026-11-10",
        "fetched_at": "2026-11-11T06:30:00+00:00",
    }
    connection = await _connection(run, cursor=cursor)
    empty = b'<FlexQueryResponse queryName="q"><FlexStatements count="0"/></FlexQueryResponse>'
    serve = _serving(lambda fd: empty)
    after = await _run_sync(session, connection, serve, tmp_path, RecordingNotifier())
    assert after.status == "failing"
    assert after.cursor == cursor
    assert after.last_error == "the Flex query returned no statements: select every account"


async def test_the_reminder_names_the_last_day_the_token_works(session, run, tmp_path) -> None:
    last_day = datetime.now(LONDON).date() + timedelta(days=10)
    # As the API stores it: the start of the next day in London.
    expires_at = datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=LONDON)
    cursor: JSONValue = {"fetched_at": datetime.now(UTC).isoformat()}
    connection = await _connection(run, cursor=cursor, expires_at=expires_at.astimezone(UTC))
    notifier = RecordingNotifier()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        await IbkrConnector().sync(await _context(session, connection, http, tmp_path, notifier))
    [message] = notifier.messages
    assert f"expires on {last_day.isoformat()}." in message


async def test_a_failing_reminder_logs_the_class_only(session, run, tmp_path, caplog) -> None:
    class Failing:
        async def send(self, text: str) -> None:
            raise RuntimeError("https://hooks.example.test/SECRET-TOKEN")

    expires_at = datetime.now(UTC) + timedelta(days=10)
    cursor: JSONValue = {"fetched_at": datetime.now(UTC).isoformat()}
    connection = await _connection(run, cursor=cursor, expires_at=expires_at)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        await IbkrConnector().sync(await _context(session, connection, http, tmp_path, Failing()))
    assert "SECRET-TOKEN" not in caplog.text
    assert "RuntimeError" in caplog.text
