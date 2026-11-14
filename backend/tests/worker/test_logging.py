import logging
from collections.abc import Iterator

import httpx
import pytest
from pydantic import SecretStr

from lararium.core.notify import FeishuNotifier
from lararium.worker.__main__ import configure_logging

HOOK_TOKEN = "hook-TEST-0123456789abcdef"
HOOK_URL = f"https://open.feishu.example/open-apis/bot/v2/hook/{HOOK_TOKEN}"
SIGNING_SECRET = "sign-TEST-secret"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    loggers = [root, logging.getLogger("httpx"), logging.getLogger("httpcore")]
    levels = [logger.level for logger in loggers]
    handlers = list(root.handlers)
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)
    root.handlers[:] = handlers


async def test_worker_logging_keeps_request_urls_out_of_the_log(
    restore_logging: None, caplog: pytest.LogCaptureFixture
) -> None:
    configure_logging()
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await FeishuNotifier(SecretStr(HOOK_URL), SecretStr(SIGNING_SECRET), http).send("hello")

    assert HOOK_TOKEN not in caplog.text
    assert SIGNING_SECRET not in caplog.text
    # The worker still logs its own INFO messages.
    logging.getLogger("lararium.worker.jobs").info("queued 1 connection syncs")
    assert "queued 1 connection syncs" in caplog.text
