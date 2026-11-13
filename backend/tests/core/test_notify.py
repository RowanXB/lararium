import base64
import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import SecretStr

from lararium.config import Settings
from lararium.core.notify import FeishuNotifier, NullNotifier, notifier_from_settings

URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_feishu_payload_and_signature() -> None:
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    async with _client(handler) as http:
        notifier = FeishuNotifier(
            SecretStr(URL), SecretStr("s3cret"), http, clock=lambda: 1700000000
        )
        await notifier.send("Monzo needs attention")

    assert len(posted) == 1
    assert str(posted[0].url) == URL
    body = json.loads(posted[0].content)
    digest = hmac.new(b"1700000000\ns3cret", b"", hashlib.sha256).digest()
    assert body == {
        "msg_type": "text",
        "content": {"text": "Monzo needs attention"},
        "timestamp": "1700000000",
        "sign": base64.b64encode(digest).decode(),
    }


async def test_feishu_without_secret_is_unsigned() -> None:
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={"code": 0})

    async with _client(handler) as http:
        await FeishuNotifier(SecretStr(URL), None, http).send("hi")

    assert json.loads(posted[0].content) == {"msg_type": "text", "content": {"text": "hi"}}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"code": 19021, "msg": "bad sign"}),
    ],
)
async def test_feishu_failure_does_not_raise(
    response: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    async with _client(lambda request: response) as http:
        await FeishuNotifier(SecretStr(URL), SecretStr("s3cret"), http).send("hi")

    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert "abc123" not in caplog.text
    assert "s3cret" not in caplog.text


async def test_feishu_network_error_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with _client(handler) as http:
        await FeishuNotifier(SecretStr(URL), None, http).send("hi")

    assert "abc123" not in caplog.text


async def test_notifier_from_settings() -> None:
    async with _client(lambda request: httpx.Response(200)) as http:
        base = Settings(database_url="postgresql+psycopg://x@localhost/x", auth_mode="dev")
        assert isinstance(notifier_from_settings(base, http), NullNotifier)
        configured = Settings(
            database_url="postgresql+psycopg://x@localhost/x",
            auth_mode="dev",
            feishu_webhook_url=SecretStr(URL),
        )
        assert isinstance(notifier_from_settings(configured, http), FeishuNotifier)
