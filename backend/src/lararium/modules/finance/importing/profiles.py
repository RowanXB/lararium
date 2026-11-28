"""Parse profiles: how to read one institution's statement files, stored as data.

A profile's config is validated here, before it is stored; parsers trust it.

A CSV profile's family_notes (account family identifier design §5.9, owner's decision
2026-11-27) reads whether a row moved money within its account's family from a column
that says so: Trading 212's History CSV gives a deposit or withdrawal between its own
accounts empty Notes, and an external one a transaction id or the bank account it went
to. The row then stores the answer typed (family_move), and commit posts a move within
the family naming the family, and one outside it as never a family move's counterpart.
Notes of another form are a row error: the source changed, and a guess could pair a bank
top-up with an internal move.

A config lists two sets of type codes, which are not the same: `card_types` are the codes
that are not bank transfers (card, cash machine, cheque, interest), for transfer matching;
`card_payment_types` are the codes of a payment made by card, which decide whether a positive
row is a card refund. A cash machine, cheque or interest code is in the first and not the
second.
"""

import re
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, get_args
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
# What a row's file gave its time to: only a date ("day": the row is at the day's midnight),
# or a time to the minute or to the second.
type TimeUnit = Literal["day", "minute", "second"]
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


def is_bank_transfer(card_types: list[str], bank_type: str | None, card_number: str | None) -> bool:
    """Whether a row with this type code, paid with this card if any, is a payment between
    bank accounts."""
    if card_number is not None:
        return False
    return bank_type is None or bank_type.strip() not in card_types


def is_card_payment(
    card_payment_types: list[str], bank_type: str | None, card_number: str | None
) -> bool:
    """Whether a row with this type code, paid with this card if any, is a card payment."""
    if card_number is not None:
        return True
    return bank_type is not None and bank_type.strip() in card_payment_types


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
    # When the payment was started (Revolut's Started Date), in date_format, which must
    # then carry a time; date is when it completed.
    started_at: str | None = None
    # The statement's own account; every row must name the same one. account_type is its
    # product line (Revolut's Product).
    account_sort_code: str | None = None
    account_number: str | None = None
    account_type: str | None = None

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
            self.started_at,
            self.account_sort_code,
            self.account_number,
            self.account_type,
        ]
        return [self.date, *self.description, *(name for name in optional if name is not None)]


# The typed fields a CSV extract may fill: CsvColumns' names for them, and the
# counterparty's bank.
type ExtractField = Literal[
    "payee",
    "memo",
    "check_number",
    "counterparty_name",
    "counterparty_sort_code",
    "counterparty_account_number",
    "counterparty_account_ending",
    "counterparty_bank",
    "reference",
]
_EXTRACT_FIELDS: frozenset[str] = frozenset(get_args(ExtractField.__value__))


def _check_pattern(pattern: str) -> str:
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"not a regular expression: {error}") from None
    names = set(compiled.groupindex)
    unknown = sorted(names - _EXTRACT_FIELDS)
    if unknown:
        raise ValueError(f"the pattern names fields it can't fill: {', '.join(unknown)}")
    return pattern


type Pattern = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_check_pattern)]


def _check_period_pattern(pattern: str) -> str:
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"not a regular expression: {error}") from None
    missing = [name for name in ("start", "end") if name not in compiled.groupindex]
    if missing:
        raise ValueError(f"the file name pattern lacks the named groups: {', '.join(missing)}")
    return pattern


type PeriodPattern = Annotated[
    str, Field(min_length=1, max_length=500), AfterValidator(_check_period_pattern)
]


class CsvExtract(BaseModel):
    """Typed fields read out of one column: the first pattern that matches a non-empty cell
    in full fills the fields its named groups name (those that match); a cell no pattern
    matches makes the row an error."""

    model_config = _CONFIG

    column: str
    patterns: list[Pattern] = Field(min_length=1, max_length=10)

    def fields(self) -> set[str]:
        return {name for pattern in self.patterns for name in re.compile(pattern).groupindex}


def _check_regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"not a regular expression: {error}") from None
    return pattern


# A pattern a whole cell is matched against; the empty one matches an empty cell.
type CellPattern = Annotated[str, Field(max_length=500), AfterValidator(_check_regex)]
type CellPatterns = Annotated[list[CellPattern], Field(min_length=1, max_length=10)]


class FamilyNotes(BaseModel):
    """Whether a row moved money within its account's family, as `column` says (account
    family identifier design §5.9): for a row whose type (the type column's value) is one
    of `types`, a stripped cell matching one of `within` in full is a move within the
    family (family_move true), one matching one of `outside` a movement outside it (false),
    and any other a row error naming the column and the rule, never the value. Every other
    row's family_move is None: the profile says nothing of it."""

    model_config = _CONFIG

    column: str
    types: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]],
        Field(min_length=1, max_length=20),
    ]
    within: CellPatterns
    outside: CellPatterns

    def family_move(self, bank_type: str | None, cell: str) -> bool | None:
        """The row's family_move; ValueError for a cell neither list matches."""
        if bank_type is None or bank_type.strip() not in self.types:
            return None
        text = cell.strip()
        if any(re.fullmatch(pattern, text) for pattern in self.within):
            return True
        if any(re.fullmatch(pattern, text) for pattern in self.outside):
            return False
        raise ValueError(
            f"{self.column}: the cell is of no form family_notes knows (within the family or "
            "outside it)"
        )


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
    card_payment_types: CardTypes = Field(default_factory=list[str])
    # A row whose description is one of these pays interest, for a bank that prints no type
    # codes (LemFi).
    interest_descriptions: list[str] = Field(default_factory=list[str])
    extract: list[CsvExtract] = Field(default_factory=list[CsvExtract], max_length=10)
    # The currency of every row, for a file that states it once, not per row (LemFi's
    # column labels, "Money in (GBP)"): rather than the one picked at upload.
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    # Searched in the uploaded file's name, for a bank that names its export by the period
    # chosen (Trading 212): named groups start and end, ISO dates (YYYY-MM-DD). A match is the
    # file's period, listed in full but for its last day, which the export may have been
    # taken partway through.
    period_from_filename: PeriodPattern | None = None
    family_notes: FamilyNotes | None = None

    def read_columns(self) -> list[str]:
        """Every column the profile reads: mapped, filtered on, extracted from or read for
        family_notes."""
        return [
            *self.columns.names(),
            *self.include_only,
            *(extract.column for extract in self.extract),
            *([] if self.family_notes is None else [self.family_notes.column]),
        ]

    @model_validator(mode="after")
    def _family_notes_need_types(self) -> Self:
        if self.family_notes is not None and self.columns.type is None:
            raise ValueError("family_notes applies to rows by their type: map the type column")
        return self

    @model_validator(mode="after")
    def _one_currency_source(self) -> Self:
        if self.currency is not None and self.columns.currency is not None:
            raise ValueError("set currency or map a currency column, not both")
        return self

    @model_validator(mode="after")
    def _extracts_fill_unmapped_fields(self) -> Self:
        mapped = {name for name in _EXTRACT_FIELDS if getattr(self.columns, name, None) is not None}
        filled: set[str] = set()
        for extract in self.extract:
            twice = sorted(extract.fields() & (mapped | filled))
            if twice:
                raise ValueError(f"fields filled twice: {', '.join(twice)}")
            filled |= extract.fields()
        return self

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
        missing = [name for name in self.read_columns() if name not in self.header]
        if missing:
            raise ValueError(f"columns not in the header: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _started_needs_time(self) -> Self:
        if self.columns.started_at is not None and not self.has_time:
            raise ValueError("started_at is a time: give date_format a time of day")
        return self

    @property
    def has_time(self) -> bool:
        """Whether the dates carry a time of day."""
        directives = self.date_format.replace("%%", "")
        return any(directive in directives for directive in _TIME_DIRECTIVES)

    @property
    def time_unit(self) -> TimeUnit:
        """What the dates give the time to: seconds when the format reads them."""
        if not self.has_time:
            return "day"
        return "second" if "%S" in self.date_format.replace("%%", "") else "minute"


class OfxProfileConfig(BaseModel):
    model_config = _CONFIG

    format: Literal["ofx"]
    sign: Sign = "inflow_positive"
    timezone: TimezoneName | None = None
    # OFX's own TRNTYPE codes for them.
    card_types: CardTypes = Field(default_factory=lambda: ["POS", "ATM", "CHECK"])
    card_payment_types: CardTypes = Field(default_factory=lambda: ["POS"])


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
    card_payment_types: CardTypes = Field(default_factory=list[str])


class PdfProfileConfig(BaseModel):
    model_config = _CONFIG

    format: Literal["pdf"]
    layout: Literal[
        "hsbc_uk_statement",
        "lloyds_statement",
        "boc_uk_statement",
        "revolut_current_statement",
        "revolut_savings_statement",
        "tembo_statement",
        "lemfi_statement",
    ]
    timezone: TimezoneName | None = None
    card_types: CardTypes = Field(default_factory=list[str])
    card_payment_types: CardTypes = Field(default_factory=list[str])


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
