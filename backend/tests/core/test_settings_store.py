import pytest

from lararium.core.errors import SettingNotFoundError
from lararium.core.settings_store import get_setting, get_timezone, set_setting


async def test_get_and_set_setting(session, run) -> None:
    assert await get_setting(session, "core.timezone") == "Europe/London"
    await run(set_setting, "core.timezone", "Asia/Shanghai")
    assert await get_setting(session, "core.timezone") == "Asia/Shanghai"
    assert str(await get_timezone(session)) == "Asia/Shanghai"


async def test_unknown_setting_raises(session) -> None:
    with pytest.raises(SettingNotFoundError):
        await get_setting(session, "nope")
