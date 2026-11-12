"""Pydantic models for the finance API. Amounts are decimal strings in JSON, never numbers."""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from lararium.modules.finance.accounts import (
    AccountStatus,
    AccountSubtype,
    BalanceMode,
    LedgerType,
    SourceKind,
    TaxWrapper,
)
from lararium.modules.finance.instruments import IdentifierScheme, InstrumentKind
from lararium.modules.finance.ledger import TransactionOrigin, TransactionStatus
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.snapshots import SnapshotKind
from lararium.modules.finance.valuation import HoldingStatus

# ASCII digits only (\d matches any script's digits), at most 40 on each side of the point:
# money.require_amount's bounds, so OpenAPI states them and the client can check them too.
DECIMAL_PATTERN = r"^-?[0-9]{1,40}(\.[0-9]{1,40})?$"
_DECIMAL_RE = re.compile(DECIMAL_PATTERN)


def _parse_decimal(value: object) -> Decimal:
    # Decimal instances come from our own records when building responses; request input
    # is decoded JSON, so it is never a Decimal and must be a matching string.
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        return Decimal(value)
    raise ValueError("must be a decimal string such as '-12.34'")


DecimalStr = Annotated[
    Decimal,
    PlainValidator(_parse_decimal),
    PlainSerializer(to_plain_str, return_type=str),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_PATTERN}),
]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class FinanceSettingsOut(BaseModel):
    reporting_currency: str


class FinanceSettingsUpdate(BaseModel):
    reporting_currency: str


class InstrumentIdentifierModel(_Out):
    scheme: IdentifierScheme
    value: str


class InstrumentCreate(BaseModel):
    kind: InstrumentKind
    symbol: str
    name: str
    decimals: int = Field(ge=0, le=30)
    quote_currency: str | None = None  # ISO 4217 code
    identifiers: list[InstrumentIdentifierModel] = []

    @model_validator(mode="after")
    def _fiat_has_no_quote_currency(self) -> Self:
        if self.kind == "fiat" and self.quote_currency is not None:
            raise ValueError("a fiat currency has no quote currency")
        return self


class InstrumentUpdate(BaseModel):
    hidden: bool


class InstrumentOut(_Out):
    id: UUID
    kind: InstrumentKind
    symbol: str
    name: str
    decimals: int
    quote_instrument_id: UUID | None
    hidden: bool
    identifiers: list[InstrumentIdentifierModel]


class AccountCreate(BaseModel):
    name: str
    ledger_type: LedgerType
    subtype: AccountSubtype
    balance_mode: BalanceMode
    institution_name: str | None = None
    tax_wrapper: TaxWrapper = "none"
    parent_account_id: UUID | None = None
    opened_on: date | None = None


class AccountRename(BaseModel):
    name: str


class AccountClose(BaseModel):
    closed_on: date


class AccountOut(_Out):
    id: UUID
    name: str
    ledger_type: LedgerType
    subtype: AccountSubtype
    institution_entity_id: UUID | None
    owner_entity_id: UUID
    tax_wrapper: TaxWrapper
    parent_account_id: UUID | None
    balance_mode: BalanceMode
    source_kind: SourceKind
    source_connection_id: UUID | None
    status: AccountStatus
    opened_on: date | None
    closed_on: date | None


class SnapshotCreate(BaseModel):
    instrument_id: UUID
    quantity: DecimalStr  # signed: negative for liabilities
    as_of: AwareDatetime


class SnapshotOut(_Out):
    id: UUID
    account_id: UUID
    instrument_id: UUID
    quantity: DecimalStr
    as_of: AwareDatetime
    as_of_date: date
    kind: SnapshotKind


class PostingCreate(BaseModel):
    account_id: UUID
    instrument_id: UUID
    quantity: DecimalStr
    cost_per_unit: DecimalStr | None = None
    cost_instrument_id: UUID | None = None
    price_per_unit: DecimalStr | None = None
    price_instrument_id: UUID | None = None
    is_fee: bool = False
    memo: str = ""

    @model_validator(mode="after")
    def _cost_and_price_are_paired(self) -> Self:
        # Mirrors the postings paired-nullability CHECKs so a half pair is a 422, not a 500.
        if (self.cost_per_unit is None) != (self.cost_instrument_id is None):
            raise ValueError("cost_per_unit and cost_instrument_id must be given together")
        if (self.price_per_unit is None) != (self.price_instrument_id is None):
            raise ValueError("price_per_unit and price_instrument_id must be given together")
        return self


class TransactionCreate(BaseModel):
    occurred_at: AwareDatetime
    description: str
    postings: list[PostingCreate] = Field(min_length=2)
    booking_date: date | None = None


class PostingOut(_Out):
    id: UUID
    position: int
    account_id: UUID
    instrument_id: UUID
    quantity: DecimalStr
    cost_per_unit: DecimalStr | None
    cost_instrument_id: UUID | None
    price_per_unit: DecimalStr | None
    price_instrument_id: UUID | None
    is_fee: bool
    memo: str


class TransactionOut(_Out):
    id: UUID
    occurred_at: AwareDatetime
    booking_date: date
    description: str
    status: TransactionStatus
    origin: TransactionOrigin
    postings: list[PostingOut]


class PriceCreate(BaseModel):
    instrument_id: UUID
    quote_currency: str  # ISO 4217 code
    price: DecimalStr
    as_of_date: date

    @field_validator("price")
    @classmethod
    def _not_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("price must not be negative")
        return value


class FxRateCreate(BaseModel):
    base: str  # ISO 4217 code
    quote: str  # ISO 4217 code
    rate: DecimalStr
    as_of_date: date

    @field_validator("rate")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("rate must be positive")
        return value

    @model_validator(mode="after")
    def _different_currencies(self) -> Self:
        if self.base.strip().upper() == self.quote.strip().upper():
            raise ValueError("base and quote must be different currencies")
        return self


class HoldingOut(_Out):
    account_id: UUID
    account_name: str
    instrument_id: UUID
    symbol: str
    quantity: DecimalStr
    unit_price: DecimalStr | None
    price_currency_id: UUID | None
    fx_rate: DecimalStr | None
    value: DecimalStr | None  # in the reporting currency; null unless status is "ok"
    status: HoldingStatus


class NetWorthPointOut(_Out):
    day: date
    value: DecimalStr
    missing_count: int
