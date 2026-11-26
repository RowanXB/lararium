"""/api/v1/finance routes. Each route's name is its OpenAPI operationId."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import command_context
from lararium.core.entities import get_or_create_organization
from lararium.core.json_types import JSONValue
from lararium.core.settings_store import get_setting, get_timezone, set_setting
from lararium.deps import ActorDep, DocumentStoreDep, SessionDep, get_actor
from lararium.modules.finance import (
    account_types,
    accounts,
    cashflow,
    errors,
    instruments,
    ledger,
    pricing,
    snapshots,
    valuation,
)
from lararium.modules.finance.importing import catalog, commit, parsed, profiles, staging
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.schemas import (
    AccountClose,
    AccountCreate,
    AccountOut,
    AccountRename,
    AccountReportingUpdate,
    AccountShapeOut,
    AccountTypeApiOut,
    AccountTypeFileOut,
    AccountTypeOut,
    CashFlowOut,
    FinanceSettingsOut,
    FinanceSettingsUpdate,
    FxRateCreate,
    HoldingOut,
    ImportBatchDetailOut,
    ImportBatchOut,
    ImportCatalogEntryOut,
    ImportProfileCreate,
    ImportProfileOut,
    ImportRowOut,
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
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers_api import healing_command

router = APIRouter(prefix="/api/v1/finance", tags=["finance"], dependencies=[Depends(get_actor)])

type ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]

# Error class → (HTTP status, machine-readable code). LedgerImbalanceError has its own handler.
_ERRORS: dict[type[errors.FinanceError], tuple[int, str]] = {
    errors.AccountNotFoundError: (status.HTTP_404_NOT_FOUND, "account_not_found"),
    errors.InstrumentNotFoundError: (status.HTTP_404_NOT_FOUND, "instrument_not_found"),
    errors.TransactionNotFoundError: (status.HTTP_404_NOT_FOUND, "transaction_not_found"),
    errors.TransferNotFoundError: (status.HTTP_404_NOT_FOUND, "transfer_not_found"),
    errors.AccountIdentifierNotFoundError: (
        status.HTTP_404_NOT_FOUND,
        "account_identifier_not_found",
    ),
    errors.DuplicateIdentifierError: (status.HTTP_409_CONFLICT, "duplicate_identifier"),
    errors.DuplicateAccountIdentifierError: (
        status.HTTP_409_CONFLICT,
        "duplicate_account_identifier",
    ),
    errors.AccountAlreadyClosedError: (status.HTTP_409_CONFLICT, "account_already_closed"),
    errors.FinanceValidationError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input"),
    errors.UnknownCurrencyError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown_currency"),
    # Not the request's fault: matching can't run until the owner fixes the setting.
    errors.TransferSettingsError: (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "invalid_transfer_settings",
    ),
    errors.NotACurrencyError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "not_a_currency"),
    errors.ImportProfileNotFoundError: (status.HTTP_404_NOT_FOUND, "import_profile_not_found"),
    errors.ImportBatchNotFoundError: (status.HTTP_404_NOT_FOUND, "import_batch_not_found"),
    errors.DuplicateProfileNameError: (status.HTTP_409_CONFLICT, "duplicate_profile_name"),
    errors.ImportNotAllowedError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "import_not_allowed"),
    errors.FileFormatError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "file_format"),
    errors.EmptyStatementError: (status.HTTP_422_UNPROCESSABLE_CONTENT, "empty_statement"),
    errors.ImportBatchStateError: (status.HTTP_409_CONFLICT, "import_batch_state"),
    errors.ImportHasErrorsError: (status.HTTP_409_CONFLICT, "import_has_errors"),
    errors.ImportBatchStaleError: (status.HTTP_409_CONFLICT, "import_batch_stale"),
    errors.TransactionImportedError: (status.HTTP_409_CONFLICT, "transaction_imported"),
    errors.ReviewNotFoundError: (status.HTTP_404_NOT_FOUND, "review_not_found"),
    errors.ReviewResolvedError: (status.HTTP_409_CONFLICT, "review_resolved"),
}
_UPLOAD_CHUNK = 64 * 1024


def _error_handler(status_code: int, code: str) -> ExceptionHandler:
    async def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": {"error": code, "message": str(exc)}}, status_code)

    return handle


async def _ledger_imbalance(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, errors.LedgerImbalanceError)
    residuals = {str(key): to_plain_str(value) for key, value in exc.residuals.items()}
    return JSONResponse(
        {"detail": {"error": "ledger_imbalance", "message": str(exc), "residuals": residuals}},
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


def _type_out(found: account_types.AccountType) -> AccountTypeOut:
    return AccountTypeOut(
        key=found.key,
        institution=found.institution,
        label=found.label,
        name=found.name,
        shape=None if found.shape is None else AccountShapeOut.model_validate(found.shape),
        api=None
        if found.api is None
        else AccountTypeApiOut(connector=found.api.connector, config=dict(found.api.config)),
        files=[
            AccountTypeFileOut(
                label=f.label, format=f.format, recommended=f.recommended, config=f.profile
            )
            for f in found.files
        ],
        funding=sorted(found.funding),
        family=found.family,
    )


@router.get("/account-types", name="list_account_types")
async def list_account_types() -> list[AccountTypeOut]:
    """The account types the owner can pick: where an account's data comes from and how it
    posts. Only a type with no API creates an account here; one with an API is connected."""
    return [_type_out(found) for found in account_types.ACCOUNT_TYPES]


def _type_error(code: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"error": code, "message": message})


def _settable_type(key: str) -> tuple[account_types.AccountType, account_types.AccountShape]:
    """The type a type_key names, with its shape; only one with no API can be set by hand."""
    found = account_types.account_type(key)
    if found is None:
        raise _type_error("unknown_type", "type_key names no account type")
    if found.api is not None or found.shape is None:
        raise _type_error(
            "type_not_creatable",
            f"{found.name} syncs through its API, so type_key can't name it; connect it instead",
        )
    return found, found.shape


def _require_type_shape(body: AccountCreate, key: str) -> account_types.AccountType:
    """The body must agree with the type wherever the type states its shape; the side is the
    body's when the type leaves it open, and the institution is always the type's."""
    found, shape = _settable_type(key)
    stated: dict[str, str | None] = {
        "ledger_type": shape.ledger_type,
        "subtype": shape.subtype,
        "tax_wrapper": shape.tax_wrapper,
        "balance_mode": shape.balance_mode,
    }
    for field, value in stated.items():
        if value is not None and getattr(body, field) != value:
            raise _type_error(
                "type_mismatch", f"{field} disagrees with the {found.name} account type"
            )
    if body.institution_name is not None:
        raise _type_error(
            "type_mismatch",
            f"institution_name is the {found.name} account type's own; leave it out",
        )
    return found


@router.get("/accounts", name="list_accounts")
async def list_accounts(session: SessionDep) -> list[AccountOut]:
    return [AccountOut.model_validate(record) for record in await accounts.list_accounts(session)]


@router.post("/accounts", name="create_account", status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountCreate, session: SessionDep, actor: ActorDep) -> AccountOut:
    found = None if body.type_key is None else _require_type_shape(body, body.type_key)
    async with command_context(session, actor, "create_account", _args(body)) as ctx:
        institution_id = None
        if found is not None:
            institution_id = await get_or_create_organization(ctx, found.institution)
        elif body.institution_name is not None:
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
            type_key=body.type_key,
        )
    return AccountOut.model_validate(record)


@router.patch("/accounts/{account_id}", name="rename_account")
async def rename_account(
    account_id: UUID, body: AccountRename, session: SessionDep, actor: ActorDep
) -> AccountOut:
    """Rename the account, set its type, or both, in one command: if either is refused,
    neither happens. A type must have no API and the account must be able to take imports;
    type_key null clears it."""
    args = _args(body, account_id=account_id)
    set_type = "type_key" in body.model_fields_set
    async with command_context(session, actor, "rename_account", args) as ctx:
        if set_type and body.type_key is not None:
            _settable_type(body.type_key)
            staging.require_importable(await accounts.get_account(session, account_id))
        if body.name is not None:
            await accounts.rename_account(ctx, account_id, body.name)
        if set_type:
            await accounts.set_type_key(ctx, account_id, body.type_key)
        record = await accounts.get_account(session, account_id)
    return AccountOut.model_validate(record)


@router.post("/accounts/{account_id}/close", name="close_account")
async def close_account(
    account_id: UUID, body: AccountClose, session: SessionDep, actor: ActorDep
) -> AccountOut:
    """Close the account; it stays a holder of the card merchants it held."""
    args = _args(body, account_id=account_id)
    async with command_context(session, actor, "close_account", args) as ctx:
        record = await accounts.close_account(ctx, account_id, body.closed_on)
    return AccountOut.model_validate(record)


@router.post("/accounts/{account_id}/reporting", name="set_account_reporting")
async def set_account_reporting(
    account_id: UUID, body: AccountReportingUpdate, session: SessionDep, actor: ActorDep
) -> AccountOut:
    """Leave the account out of net worth, holdings and cash flow, or put it back."""
    args = _args(body, account_id=account_id)
    async with command_context(session, actor, "set_account_reporting", args) as ctx:
        record = await accounts.set_excluded_from_reports(ctx, account_id, body.excluded)
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
    # A manual hybrid account's first snapshot settles the transfers it contains (heal).
    async with healing_command(session, actor, "record_snapshot", args) as ctx:
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
    # Only matching posts on In transit (a transfer's legs and placeholders); a hand-posted
    # posting there would sit in it with no transfer behind it.
    in_transit = (await system_account_ids(session)).get("in_transit")
    if any(p.account_id == in_transit for p in body.postings):
        raise errors.FinanceValidationError(
            "In transit holds only transfers between your accounts, which are matched"
            " automatically; post against another account"
        )
    async with command_context(session, actor, "post_transaction", _args(body)) as ctx:
        record = await ledger.post_transaction(ctx, tx)
    return TransactionOut.model_validate(record)


@router.post("/transactions/{transaction_id}/void", name="void_transaction")
async def void_transaction(
    transaction_id: UUID, session: SessionDep, actor: ActorDep
) -> TransactionOut:
    """Void the transaction; a transfer it was a side of follows (heal)."""
    args = _args(transaction_id=transaction_id)
    async with healing_command(session, actor, "void_transaction", args) as ctx:
        await staging.require_not_imported(ctx.session, transaction_id)
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
    session: SessionDep,
    day: Annotated[date | None, Query(alias="date")] = None,
    all_accounts: bool = False,
) -> list[HoldingOut]:
    """Holdings valued in the reporting currency, without the accounts left out of reports
    unless `all_accounts`; the date defaults to today."""
    excluded: frozenset[UUID] = (
        frozenset() if all_accounts else await accounts.excluded_account_ids(session)
    )
    rows = await valuation.holdings_asof(session, day or await _today(session), excluded)
    return [HoldingOut.model_validate(row) for row in rows]


async def _year_to(
    session: AsyncSession, start: date | None, end: date | None
) -> tuple[date, date]:
    """The range asked for; by default the 365 days up to and including today."""
    end = end or await _today(session)
    if start is None:
        # Clamped, because a year before the first representable day would overflow.
        start = end - timedelta(days=364) if end.toordinal() > 364 else date.min
    return start, end


@router.get("/net-worth", name="get_net_worth")
async def get_net_worth(
    session: SessionDep, start: date | None = None, end: date | None = None
) -> list[NetWorthPointOut]:
    """Daily net worth, without the accounts left out of reports; defaults to the 365 days up
    to and including today."""
    start, end = await _year_to(session, start, end)
    excluded = await accounts.excluded_account_ids(session)
    points = await valuation.net_worth_series(session, start, end, excluded)
    return [NetWorthPointOut.model_validate(point) for point in points]


@router.get("/cash-flow", name="get_cash_flow")
async def get_cash_flow(
    session: SessionDep,
    start: date | None = None,
    end: date | None = None,
    bucket: cashflow.Bucket = "month",
) -> CashFlowOut:
    """Income and spending by period, category and payee, without the accounts left out of
    reports; defaults to the 365 days up to and including today."""
    start, end = await _year_to(session, start, end)
    excluded = await accounts.excluded_account_ids(session)
    return CashFlowOut.model_validate(
        await cashflow.cash_flow(session, start, end, bucket, excluded)
    )


# Imports


@router.get("/import-profiles", name="list_import_profiles")
async def list_import_profiles(session: SessionDep) -> list[ImportProfileOut]:
    return [ImportProfileOut.model_validate(r) for r in await profiles.list_profiles(session)]


@router.post("/import-profiles", name="create_import_profile", status_code=status.HTTP_201_CREATED)
async def create_import_profile(
    body: ImportProfileCreate, session: SessionDep, actor: ActorDep
) -> ImportProfileOut:
    async with command_context(session, actor, "create_import_profile", _args(body)) as ctx:
        record = await profiles.create_profile(ctx, name=body.name, config=body.config)
    return ImportProfileOut.model_validate(record)


@router.put("/import-profiles/{profile_id}", name="update_import_profile")
async def update_import_profile(
    profile_id: UUID, body: ImportProfileCreate, session: SessionDep, actor: ActorDep
) -> ImportProfileOut:
    args = _args(body, profile_id=profile_id)
    async with command_context(session, actor, "update_import_profile", args) as ctx:
        record = await profiles.update_profile(ctx, profile_id, name=body.name, config=body.config)
    return ImportProfileOut.model_validate(record)


@router.get("/import-catalog", name="get_import_catalog")
async def get_import_catalog() -> list[ImportCatalogEntryOut]:
    """Each supported bank's export formats with a ready profile config (one recommended)."""
    return [ImportCatalogEntryOut.model_validate(entry) for entry in catalog.CATALOG]


async def read_upload(file: UploadFile, limit: int = parsed.MAX_IMPORT_BYTES) -> bytes:
    """The upload's content, read in chunks: 413 as soon as it passes `limit` (M2's
    MAX_IMPORT_BYTES unless a route has its own).

    This bounds what is parsed and stored, not what is received: Starlette has already
    spooled the whole multipart body to a temporary file before the route runs (and before
    the cross-site check's 403).
    """
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(_UPLOAD_CHUNK):
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                {
                    "error": "file_too_large",
                    "message": f"the file is larger than {limit} bytes",
                },
            )
        chunks.append(chunk)
    if size == 0:
        raise errors.FileFormatError("the file is empty")
    return b"".join(chunks)


async def _batch_detail(
    session: AsyncSession, record: staging.ImportBatchRecord
) -> ImportBatchDetailOut:
    rows = await staging.list_batch_rows(session, record.id)
    batch = ImportBatchOut.model_validate(record)
    return ImportBatchDetailOut(
        **dict(batch), rows=[ImportRowOut.model_validate(row) for row in rows]
    )


@router.post("/imports", name="stage_import", status_code=status.HTTP_201_CREATED)
async def stage_import(
    account_id: Annotated[UUID, Form()],
    profile_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
    session: SessionDep,
    actor: ActorDep,
    store: DocumentStoreDep,
    instrument_id: Annotated[UUID | None, Form()] = None,
) -> ImportBatchDetailOut:
    """Upload a statement file and stage it for review; nothing is posted until commit."""
    content = await read_upload(file)
    filename = file.filename or "upload"
    # The audit records which file, never its content.
    args: dict[str, JSONValue] = {
        "account_id": str(account_id),
        "profile_id": str(profile_id),
        "instrument_id": None if instrument_id is None else str(instrument_id),
        "filename": filename,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    async with command_context(session, actor, "stage_import", args) as ctx:
        record = await staging.stage_import(
            ctx,
            store,
            account_id=account_id,
            profile_id=profile_id,
            content=content,
            filename=filename,
            media_type=file.content_type or "application/octet-stream",
            default_instrument_id=instrument_id,
        )
    return await _batch_detail(session, record)


@router.get("/imports", name="list_import_batches")
async def list_import_batches(session: SessionDep) -> list[ImportBatchOut]:
    return [ImportBatchOut.model_validate(r) for r in await staging.list_batches(session)]


@router.get("/imports/{batch_id}", name="get_import_batch")
async def get_import_batch(batch_id: UUID, session: SessionDep) -> ImportBatchDetailOut:
    return await _batch_detail(session, await staging.get_batch(session, batch_id))


@router.post("/imports/{batch_id}/commit", name="commit_import")
async def commit_import(batch_id: UUID, session: SessionDep, actor: ActorDep) -> ImportBatchOut:
    args = _args(batch_id=batch_id)
    async with command_context(session, actor, "commit_import", args) as ctx:
        record = await commit.commit_batch(ctx, batch_id)
    return ImportBatchOut.model_validate(record)


@router.post("/imports/{batch_id}/discard", name="discard_import")
async def discard_import(batch_id: UUID, session: SessionDep, actor: ActorDep) -> ImportBatchOut:
    args = _args(batch_id=batch_id)
    async with command_context(session, actor, "discard_import", args) as ctx:
        record = await commit.discard_batch(ctx, batch_id)
    return ImportBatchOut.model_validate(record)


@router.post("/imports/{batch_id}/rollback", name="rollback_import")
async def rollback_import(batch_id: UUID, session: SessionDep, actor: ActorDep) -> ImportBatchOut:
    args = _args(batch_id=batch_id)
    async with command_context(session, actor, "rollback_import", args) as ctx:
        record = await commit.rollback_batch(ctx, batch_id)
    return ImportBatchOut.model_validate(record)
