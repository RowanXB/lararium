"""/api/v1 routes owned by core. Each route's name is its OpenAPI operationId."""

from functools import cache
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from financial_gate.core.commands import command_context
from financial_gate.core.settings_store import TIMEZONE_KEY, get_timezone, set_setting
from financial_gate.deps import ActorDep, SessionDep, get_actor

public_router = APIRouter(prefix="/api/v1", tags=["core"])
router = APIRouter(prefix="/api/v1", tags=["core"], dependencies=[Depends(get_actor)])


@cache
def _timezone_names() -> frozenset[str]:
    return frozenset(available_timezones())


class HealthOut(BaseModel):
    status: str


class SettingsOut(BaseModel):
    timezone: str


class SettingsUpdate(BaseModel):
    timezone: str  # IANA name, e.g. "Europe/London"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        if value not in _timezone_names():
            raise ValueError("must be an IANA time zone name, such as 'Europe/London'")
        return value


@public_router.get("/health", name="health")
async def health() -> HealthOut:
    return HealthOut(status="ok")


@router.get("/settings", name="get_settings")
async def get_settings(session: SessionDep) -> SettingsOut:
    return SettingsOut(timezone=(await get_timezone(session)).key)


@router.put("/settings", name="update_settings")
async def update_settings(
    body: SettingsUpdate, session: SessionDep, actor: ActorDep
) -> SettingsOut:
    async with command_context(
        session, actor, "update_settings", body.model_dump(mode="json")
    ) as ctx:
        await set_setting(ctx, TIMEZONE_KEY, body.timezone)
    return SettingsOut(timezone=body.timezone)
