"""Parse profiles: how to read one institution's statement files, stored as data.

A profile's config is validated here, before it is stored; parsers trust it.
"""

import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.errors import UniqueViolation
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)
from sqlalchemy import ColumnElement, Insert, Update, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.errors import (
    DuplicateProfileNameError,
    FinanceValidationError,
    ImportProfileNotFoundError,
)

_NAME_INDEX = "import_profiles_name_unique"
# The csv module quotes with '"' and splits records on line breaks.
_FORBIDDEN_DELIMITERS = frozenset({'"', "\n", "\r"})
_TIME_DIRECTIVES = ("%H", "%I", "%M", "%S", "%p")
# Formatted with a date format and parsed back: the format must keep day, month and year.
_PROBE = datetime(2026, 12, 31, 23, 59, 58, tzinfo=UTC)


def _check_timezone(name: str) -> str:
    # Pydantic reports only ValueError as a validation error; ZoneInfo can also raise
    # OSError (a directory such as "Europe") and more, so any failure means "not a zone".
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown time zone: {name}") from None
    return name


def _check_date_format(date_format: str) -> str:
    # Any failure means "not a usable format", and must surface as a ValueError: strptime
    # also raises re.error (a repeated directive), and a year-less format warns (and may
    # raise from Python 3.15).
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            text = _PROBE.strftime(date_format)
            parsed = datetime.strptime(text, date_format).replace(tzinfo=UTC)
    except Exception:
        parsed = None
    if parsed is None or parsed.date() != _PROBE.date():
        raise ValueError(f"the date format {date_format!r} doesn't keep the day, month and year")
    return date_format


type TimezoneName = Annotated[str, AfterValidator(_check_timezone)]
type DateFormat = Annotated[str, AfterValidator(_check_date_format)]


def _check_delimiter(delimiter: str) -> str:
    if delimiter in _FORBIDDEN_DELIMITERS:
        raise ValueError("the delimiter can't be a quote or a line break")
    return delimiter


type Delimiter = Annotated[str, Field(min_length=1, max_length=1), AfterValidator(_check_delimiter)]
type Encoding = Literal["utf-8-sig", "utf-8", "cp1252", "latin-1"]
type Sign = Literal["inflow_positive", "outflow_positive"]


def _check_label(label: str) -> str:
    if not label.strip():
        raise ValueError("the footer label can't be empty")
    return label


type FooterLabel = Annotated[str, AfterValidator(_check_label)]

_CONFIG = ConfigDict(extra="forbid", frozen=True)


# A bank's type codes for payments that are not between bank accounts: card, cash and
# cheque payments (M4 transfer matching, design §5.4). Every other row, a row without a type
# included, is a bank transfer: matching then only pairs it with a transfer whose other side
# names this account, on days the account's imports list in full.
type CardTypes = Annotated[
    list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]],
    Field(max_length=50),
]


def is_bank_transfer(card_types: list[str], bank_type: str | None) -> bool:
    """Whether a row with this type code is a payment between bank accounts."""
    return bank_type is None or bank_type.strip() not in card_types


class AmountFormat(BaseModel):
    model_config = _CONFIG

    decimal_separator: Literal[".", ","] = "."
    thousands_separator: Literal["", ",", ".", " ", "'"] = ""

    @model_validator(mode="after")
    def _separators_differ(self) -> Self:
        if self.decimal_separator == self.thousands_separator:
            raise ValueError("the decimal and thousands separators must differ")
        return self


class CsvColumns(BaseModel):
    """Column names in the file; several description columns are joined with one space."""

    model_config = _CONFIG

    date: str
    description: list[str] = Field(min_length=1)
    amount: str | None = None
    inflow: str | None = None
    outflow: str | None = None
    balance: str | None = None
    currency: str | None = None
    external_id: str | None = None
    fee: str | None = None
    type: str | None = None  # the bank's transaction type
    value_date: str | None = None
    payee: str | None = None
    memo: str | None = None
    check_number: str | None = None
    counterparty_name: str | None = None
    counterparty_sort_code: str | None = None
    counterparty_account_number: str | None = None
    reference: str | None = None  # the payment's reference, part of the counterparty
    # The statement's own account; every row must name the same one.
    account_sort_code: str | None = None
    account_number: str | None = None

    @model_validator(mode="after")
    def _amount_xor_inflow_outflow(self) -> Self:
        signed = self.amount is not None
        split = self.inflow is not None and self.outflow is not None
        partial = (self.inflow is None) != (self.outflow is None)
        if signed == split or partial:
            raise ValueError("map either amount, or both inflow and outflow")
        return self

    def names(self) -> list[str]:
        """Every column this maps."""
        optional = [
            self.amount,
            self.inflow,
            self.outflow,
            self.balance,
            self.currency,
            self.external_id,
            self.fee,
            self.type,
            self.value_date,
            self.payee,
            self.memo,
            self.check_number,
            self.counterparty_name,
            self.counterparty_sort_code,
            self.counterparty_account_number,
            self.reference,
            self.account_sort_code,
            self.account_number,
        ]
        return [self.date, *self.description, *(name for name in optional if name is not None)]


class CsvProfileConfig(BaseModel):
    model_config = _CONFIG

    format: Literal["csv"]
    encoding: Encoding = "utf-8-sig"
    delimiter: Delimiter = ","
    skip_rows: int = Field(default=0, ge=0, le=50)
    # Column names, in order, for a file without a header row; None: the first row is the header.
    header: list[str] | None = Field(default=None, min_length=1)
    skip_footer_rows: int = Field(default=0, ge=0, le=20)
    # The first cell of the footer line that states the arranged overdraft limit (midata).
    overdraft_footer_label: FooterLabel | None = None
    columns: CsvColumns
    date_format: DateFormat
    timezone: TimezoneName | None = None
    include_only: dict[str, Annotated[list[str], Field(min_length=1)]] = Field(
        default_factory=dict[str, list[str]]
    )
    row_order: Literal["oldest_first", "newest_first"] = "oldest_first"
    sign: Sign = "inflow_positive"
    amount_format: AmountFormat = AmountFormat()
    card_types: CardTypes = Field(default_factory=list[str])

    @model_validator(mode="after")
    def _label_needs_footer(self) -> Self:
        if self.overdraft_footer_label is not None and self.skip_footer_rows == 0:
            raise ValueError(
                "overdraft_footer_label is looked for in the footer: set skip_footer_rows"
            )
        return self

    @model_validator(mode="after")
    def _columns_in_header(self) -> Self:
        if self.header is None:
            return self
        missing = [
            name for name in [*self.columns.names(), *self.include_only] if name not in self.header
        ]
        if missing:
            raise ValueError(f"columns not in the header: {', '.join(missing)}")
        return self

    @property
    def has_time(self) -> bool:
        """Whether the dates carry a time of day."""
        directives = self.date_format.replace("%%", "")
        return any(directive in directives for directive in _TIME_DIRECTIVES)


class OfxProfileConfig(BaseModel):
    model_config = _CONFIG

    format: Literal["ofx"]
    sign: Sign = "inflow_positive"
    timezone: TimezoneName | None = None
    # OFX's own TRNTYPE codes for them.
    card_types: CardTypes = Field(default_factory=lambda: ["POS", "ATM", "CHECK"])


class QifProfileConfig(BaseModel):
    model_config = _CONFIG

    format: Literal["qif"]
    date_format: DateFormat
    encoding: Encoding = "utf-8-sig"
    # QIF amounts are written in the bank's locale.
    amount_format: AmountFormat = AmountFormat(thousands_separator=",")
    sign: Sign = "inflow_positive"
    timezone: TimezoneName | None = None
    card_types: CardTypes = Field(default_factory=list[str])


class PdfProfileConfig(BaseModel):
    model_config = _CONFIG

    format: Literal["pdf"]
    layout: Literal["hsbc_uk_statement", "lloyds_statement", "boc_uk_statement"]
    timezone: TimezoneName | None = None
    card_types: CardTypes = Field(default_factory=list[str])


type ProfileConfigModel = CsvProfileConfig | OfxProfileConfig | QifProfileConfig | PdfProfileConfig
type ProfileConfig = Annotated[
    CsvProfileConfig | OfxProfileConfig | QifProfileConfig | PdfProfileConfig,
    Field(discriminator="format"),
]

_PROFILE_CONFIG: TypeAdapter[ProfileConfigModel] = TypeAdapter(ProfileConfig)


@dataclass(frozen=True)
class ImportProfileRecord:
    id: UUID
    name: str
    config: ProfileConfigModel
    version: int
    updated_at: datetime


async def _load(
    session: AsyncSession, *conditions: ColumnElement[bool]
) -> list[ImportProfileRecord]:
    """Profiles matching all conditions, sorted by name ignoring case (then id)."""
    p = models.ImportProfile
    rows = await session.execute(
        select(p.id, p.name, p.config, p.version, p.updated_at)
        .where(*conditions)
        .order_by(func.lower(p.name), p.id)
    )
    return [
        ImportProfileRecord(
            id=row.id,
            name=row.name,
            config=_PROFILE_CONFIG.validate_python(row.config),
            version=row.version,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def _clean_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise FinanceValidationError("an import profile needs a name")
    return stripped


def _config_data(config: ProfileConfigModel) -> dict[str, JSONValue]:
    return config.model_dump(mode="json")


def _publish(ctx: CommandContext, event_type: str, record: ImportProfileRecord) -> None:
    ctx.emit(event_type, record.id, {"profile_id": str(record.id)})
    ctx.record_change(
        f"import_profile:{record.id}",
        {
            "id": str(record.id),
            "name": record.name,
            "config": _config_data(record.config),
            "version": record.version,
        },
    )


async def _write_named(session: AsyncSession, name: str, statement: Insert | Update) -> UUID | None:
    """Run an insert or update that returns the id; a clash on the name index is a domain error."""
    try:
        return await session.scalar(statement)
    except IntegrityError as error:
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if isinstance(error.orig, UniqueViolation) and constraint == _NAME_INDEX:
            raise DuplicateProfileNameError(name) from error
        raise


async def get_profile(session: AsyncSession, profile_id: UUID) -> ImportProfileRecord:
    found = await _load(session, models.ImportProfile.id == profile_id)
    if not found:
        raise ImportProfileNotFoundError(str(profile_id))
    return found[0]


async def list_profiles(session: AsyncSession) -> list[ImportProfileRecord]:
    return await _load(session)


async def create_profile(
    ctx: CommandContext, *, name: str, config: ProfileConfigModel
) -> ImportProfileRecord:
    session = ctx.session
    name = _clean_name(name)
    statement = (
        insert(models.ImportProfile)
        .values(
            name=name,
            format=config.format,
            config=_config_data(config),
            owner_entity_id=await get_self_entity_id(session),
        )
        .returning(models.ImportProfile.id)
    )
    profile_id = await _write_named(session, name, statement)
    assert profile_id is not None
    record = await get_profile(session, profile_id)
    _publish(ctx, "finance.import_profile.created", record)
    return record


async def update_profile(
    ctx: CommandContext, profile_id: UUID, *, name: str, config: ProfileConfigModel
) -> ImportProfileRecord:
    session = ctx.session
    name = _clean_name(name)
    p = models.ImportProfile
    statement = (
        update(p)
        .where(p.id == profile_id)
        .values(
            name=name,
            format=config.format,
            config=_config_data(config),
            version=p.version + 1,
            updated_at=func.now(),
        )
        .returning(p.id)
    )
    if await _write_named(session, name, statement) is None:
        raise ImportProfileNotFoundError(str(profile_id))
    record = await get_profile(session, profile_id)
    _publish(ctx, "finance.import_profile.updated", record)
    return record
