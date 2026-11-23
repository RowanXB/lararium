"""Review items between two sources of one event, and the owner's decisions (Decision 14)."""

import pytest

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import create_connection
from lararium.modules.finance import source_reviews
from lararium.modules.finance.errors import ReviewNotFoundError, ReviewResolvedError

EVENT = "close:3000000001"


@pytest.fixture
async def connection_id(run):
    connection = await run(create_connection, connector="etoro", display_name="eToro", config={})
    return connection.id


async def _open(run, connection_id, api: str = "4", statement: str = "5", field: str = "units"):
    return await run(source_reviews.open_review, connection_id, None, EVENT, field, api, statement)


async def test_open_once_then_listed(session, run, connection_id) -> None:
    assert await _open(run, connection_id) is True
    # The same disagreement again: nothing new to tell.
    assert await _open(run, connection_id) is False
    [item] = await source_reviews.list_reviews(session, connection_id)
    assert (item.event_key, item.field, item.api_value, item.statement_value) == (
        EVENT,
        "units",
        "4",
        "5",
    )
    assert item.resolution is None and item.resolved_at is None


async def test_a_changed_value_updates_the_open_item(session, run, connection_id) -> None:
    await _open(run, connection_id)
    assert await _open(run, connection_id, statement="6") is True
    [item] = await source_reviews.list_reviews(session, connection_id)
    assert item.statement_value == "6"


async def test_resolving_records_the_decision(session, run, connection_id) -> None:
    await _open(run, connection_id)
    [item] = await source_reviews.list_reviews(session, connection_id)
    assert await source_reviews.decision_for(session, connection_id, EVENT, "units") is None
    owner = Actor("user", "owner")
    async with command_context(session, owner, "resolve", {}) as ctx:
        await source_reviews.resolve_review(ctx, item.id, "statement")
    assert await source_reviews.decision_for(session, connection_id, EVENT, "units") == "statement"
    [resolved] = await source_reviews.list_reviews(session, connection_id)
    assert resolved.resolution == "statement" and resolved.resolved_by == "user"
    # The decided disagreement does not reopen; a new value does.
    assert await _open(run, connection_id) is False
    assert await _open(run, connection_id, statement="7") is True
    async with command_context(session, owner, "resolve", {}) as ctx:
        with pytest.raises(ReviewResolvedError):
            await source_reviews.resolve_review(ctx, item.id, "api")


async def test_resolving_an_unknown_item_fails(session, run, connection_id) -> None:
    async with command_context(session, Actor("user", "owner"), "resolve", {}) as ctx:
        with pytest.raises(ReviewNotFoundError):
            await source_reviews.resolve_review(ctx, connection_id, "api")
