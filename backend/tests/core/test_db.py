from sqlalchemy import text


async def test_statements_time_out(session) -> None:
    # Bounds how long any one request can hold a connection, whatever query it runs.
    assert await session.scalar(text("SHOW statement_timeout")) == "30s"


async def test_statements_are_not_jit_compiled(session) -> None:
    # Compiling cost more than running heal's queries did (tests/finance/transfers/
    # test_performance.py).
    assert await session.scalar(text("SHOW jit")) == "off"
