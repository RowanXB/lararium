"""/api/v1/finance routes. Each route's name is its OpenAPI operationId."""

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from financial_gate.core.commands import command_context
from financial_gate.core.entities import get_or_create_organization
from financial_gate.core.json_types import JSONValue
from financial_gate.core.settings_store import get_setting, get_timezone, set_setting
from financial_gate.deps import ActorDep, SessionDep, get_actor
from financial_gate.modules.finance import (
    accounts,
    errors,
    instruments,
    ledger,
    pricing,
    snapshots,
    valuation,
)
from financial_gate.modules.finance.money import to_plain_str
from financial_gate.modules.finance.schemas import (
    AccountClose,
    AccountCreate,
    AccountOut,
    AccountRename,
    FinanceSettingsOut,
    FinanceSettingsUpdate,
    FxRateCreate,
    HoldingOut,
    InstrumentCreate,
    InstrumentOut,
    InstrumentUpdate,
    NetWorthPointOut,
    PriceCreate,
    SnapshotCreate,
    SnapshotOut,
    TransactionCreate,
    TransactionOut,
)

router = APIRouter(prefix="/api/v1/finance", tags=["finance"], dependencies=[Depends(get_actor)])

type ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]

# Error class → (HTTP status, machine-readable code). LedgerImbalanceError has its own handler.
_ERRORS: dict[type[errors.FinanceError], tuple[int, str]] = {
    errors.AccountNotFoundError: (status.HTTP_404_NOT_FOUND, "account_not_found"),
    errors.InstrumentNotFoundError: (status.HTTP_404_NOT_FOUND, "instrument_not_found"),
    errors.TransactionNotFoundError: (status.HTTP_404_NOT_FOUND, "transaction_not_found"),
    errors.DuplicateIdentifierError: (status.HTTP_409_CONFLICT, "duplicate_identifier"),
    errors.AccountAlreadyClosedError: (status.HTTP_409_CONFLICT, "account_already_closed"),
    errors.UnknownCurrencyError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown_currency"),
    errors.NotACurrencyError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "not_a_currency"),
}


def _error_handler(status_code: int, code: str) -> ExceptionHandler:
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": {"error": code, "message": str(exc)}}, status_code)

    return handle


async def _ledger_imbalance(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, errors.LedgerImbalanceError)
    residuals = {str(key): to_plain_str(value) for key, value in exc.residuals.items()}
    return JSONResponse(
        {"detail": {"error": "ledger_imbalance", "residuals": residuals}},
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


def install(app: FastAPI) -> None:
    app.include_router(router)
    for error_class, (status_code, code) in _ERRORS.items():
        app.add_exception_handler(error_class, _error_handler(status_code, code))
    app.add_exception_handler(errors.LedgerImbalanceError, _ledger_imbalance)


async def _today(session: AsyncSession) -> date:
    return datetime.now(await get_timezone(session)).date()


def _args(body: BaseModel | None = None, **path: UUID) -> dict[str, JSONValue]:
    """Audit args: path ids plus the request body, as JSON."""
    args: dict[str, JSONValue] = {key: str(value) for key, value in path.items()}
    if body is not None:
        args |= body.model_dump(mode="json")
    return args


# Settings


@router.get("/settings", name="get_finance_settings")
async def get_finance_settings(session: SessionDep) -> FinanceSettingsOut:
    value = await get_setting(session, instruments.REPORTING_CURRENCY_KEY)
    return FinanceSettingsOut(reporting_currency=str(value))


@router.put("/settings", name="update_finance_settings")
async def update_finance_settings(
    body: FinanceSettingsUpdate, session: SessionDep, actor: ActorDep
) -> FinanceSettingsOut:
    async with command_context(session, actor, "update_finance_settings", _args(body)) as ctx:
        currency = await instruments.get_currency(session, body.reporting_currency)
        if currency.kind != "fiat":
            raise errors.NotACurrencyError(body.reporting_currency)
        code = instruments.normalize_identifier("iso4217", body.reporting_currency)
        await set_setting(ctx, instruments.REPORTING_CURRENCY_KEY, code)
    return FinanceSettingsOut(reporting_currency=code)


# Instruments


@router.get("/instruments", name="list_instruments")
async def list_instruments(
    session: SessionDep, include_hidden: bool = False
) -> list[InstrumentOut]:
    found = await instruments.list_instruments(session, include_hidden=include_hidden)
    return [InstrumentOut.model_validate(record) for record in found]


@router.post("/instruments", name="create_instrument", status_code=status.HTTP_201_CREATED)
async def create_instrument(
    body: InstrumentCreate, session: SessionDep, actor: ActorDep
) -> InstrumentOut:
    async with command_context(session, actor, "create_instrument", _args(body)) as ctx:
        quote_id = None
        if body.quote_currency is not None:
            quote_id = (await instruments.get_currency(session, body.quote_currency)).id
        record = await instruments.create_instrument(
            ctx,
            kind=body.kind,
            symbol=body.symbol,
            name=body.name,
            decimals=body.decimals,
            quote_instrument_id=quote_id,
            identifiers=[
                instruments.InstrumentIdentifier(i.scheme, i.value) for i in body.identifiers
            ],
        )
    return InstrumentOut.model_validate(record)


@router.patch("/instruments/{instrument_id}", name="update_instrument")
async def update_instrument(
    instrument_id: UUID, body: InstrumentUpdate, session: SessionDep, actor: ActorDep
) -> InstrumentOut:
    args = _args(body, instrument_id=instrument_id)
    async with command_context(session, actor, "update_instrument", args) as ctx:
        record = await instruments.set_hidden(ctx, instrument_id, body.hidden)
    return InstrumentOut.model_validate(record)


# Accounts


@router.get("/accounts", name="list_accounts")
async def list_accounts(session: SessionDep) -> list[AccountOut]:
    return [AccountOut.model_validate(record) for record in await accounts.list_accounts(session)]


@router.post("/accounts", name="create_account", status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountCreate, session: SessionDep, actor: ActorDep) -> AccountOut:
    async with command_context(session, actor, "create_account", _args(body)) as ctx:
        institution_id = None
        if body.institution_name is not None:
            institution_id = await get_or_create_organization(ctx, body.institution_name)
        record = await accounts.create_account(
            ctx,
            name=body.name,
            ledger_type=body.ledger_type,
            subtype=body.subtype,
            balance_mode=body.balance_mode,
            institution_entity_id=institution_id,
            tax_wrapper=body.tax_wrapper,
            parent_account_id=body.parent_account_id,
            opened_on=body.opened_on,
        )
    return AccountOut.model_validate(record)


@router.patch("/accounts/{account_id}", name="rename_account")
async def rename_account(
    account_id: UUID, body: AccountRename, session: SessionDep, actor: ActorDep
) -> AccountOut:
    args = _args(body, account_id=account_id)
    async with command_context(session, actor, "rename_account", args) as ctx:
        record = await accounts.rename_account(ctx, account_id, body.name)
    return AccountOut.model_validate(record)


@router.post("/accounts/{account_id}/close", name="close_account")
async def close_account(
    account_id: UUID, body: AccountClose, session: SessionDep, actor: ActorDep
) -> AccountOut:
    args = _args(body, account_id=account_id)
    async with command_context(session, actor, "close_account", args) as ctx:
        record = await accounts.close_account(ctx, account_id, body.closed_on)
    return AccountOut.model_validate(record)


@router.post(
    "/accounts/{account_id}/snapshots",
    name="record_snapshot",
    status_code=status.HTTP_201_CREATED,
)
async def record_snapshot(
    account_id: UUID, body: SnapshotCreate, session: SessionDep, actor: ActorDep
) -> SnapshotOut:
    args = _args(body, account_id=account_id)
    async with command_context(session, actor, "record_snapshot", args) as ctx:
        record = await snapshots.record_snapshot(
            ctx,
            account_id=account_id,
            instrument_id=body.instrument_id,
            quantity=body.quantity,
            as_of=body.as_of,
        )
    return SnapshotOut.model_validate(record)


# Transactions


@router.post("/transactions", name="post_transaction", status_code=status.HTTP_201_CREATED)
async def post_transaction(
    body: TransactionCreate, session: SessionDep, actor: ActorDep
) -> TransactionOut:
    tx = ledger.TransactionInput(
        occurred_at=body.occurred_at,
        description=body.description,
        booking_date=body.booking_date,
        postings=[
            ledger.PostingInput(
                account_id=p.account_id,
                instrument_id=p.instrument_id,
                quantity=p.quantity,
                cost_per_unit=p.cost_per_unit,
                cost_instrument_id=p.cost_instrument_id,
                price_per_unit=p.price_per_unit,
                price_instrument_id=p.price_instrument_id,
                is_fee=p.is_fee,
                memo=p.memo,
            )
            for p in body.postings
        ],
    )
    async with command_context(session, actor, "post_transaction", _args(body)) as ctx:
        record = await ledger.post_transaction(ctx, tx)
    return TransactionOut.model_validate(record)


@router.post("/transactions/{transaction_id}/void", name="void_transaction")
async def void_transaction(
    transaction_id: UUID, session: SessionDep, actor: ActorDep
) -> TransactionOut:
    args = _args(transaction_id=transaction_id)
    async with command_context(session, actor, "void_transaction", args) as ctx:
        record = await ledger.void_transaction(ctx, transaction_id)
    return TransactionOut.model_validate(record)


# Prices and FX rates


@router.post("/prices", name="record_price", status_code=status.HTTP_204_NO_CONTENT)
async def record_price(body: PriceCreate, session: SessionDep, actor: ActorDep) -> None:
    async with command_context(session, actor, "record_price", _args(body)) as ctx:
        quote = await instruments.get_currency(session, body.quote_currency)
        await pricing.record_price(
            ctx,
            instrument_id=body.instrument_id,
            quote_instrument_id=quote.id,
            price=body.price,
            as_of_date=body.as_of_date,
        )


@router.post("/fx-rates", name="record_fx_rate", status_code=status.HTTP_204_NO_CONTENT)
async def record_fx_rate(body: FxRateCreate, session: SessionDep, actor: ActorDep) -> None:
    async with command_context(session, actor, "record_fx_rate", _args(body)) as ctx:
        base = await instruments.get_currency(session, body.base)
        quote = await instruments.get_currency(session, body.quote)
        await pricing.record_fx_rate(
            ctx,
            base_instrument_id=base.id,
            quote_instrument_id=quote.id,
            rate=body.rate,
            as_of_date=body.as_of_date,
        )


# Valuation


@router.get("/holdings", name="get_holdings")
async def get_holdings(
    session: SessionDep, day: Annotated[date | None, Query(alias="date")] = None
) -> list[HoldingOut]:
    """Holdings valued in the reporting currency; the date defaults to today."""
    rows = await valuation.holdings_asof(session, day or await _today(session))
    return [HoldingOut.model_validate(row) for row in rows]


@router.get("/net-worth", name="get_net_worth")
async def get_net_worth(
    session: SessionDep, start: date | None = None, end: date | None = None
) -> list[NetWorthPointOut]:
    """Daily net worth; defaults to the 365 days up to and including today."""
    end = end or await _today(session)
    start = start or end - timedelta(days=364)
    points = await valuation.net_worth_series(session, start, end)
    return [NetWorthPointOut.model_validate(point) for point in points]
