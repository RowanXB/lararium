from sqlalchemy import text


async def test_statements_time_out(session) -> None:
    # Bounds how long any one request can hold a connection, whatever query it runs.
    assert await session.scalar(text("SHOW statement_timeout")) == "30s"
