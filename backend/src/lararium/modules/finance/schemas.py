"""Pydantic models for the finance API. Amounts are decimal strings in JSON, never numbers."""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self
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
from lararium.modules.finance.identifiers import IdentifierKind
from lararium.modules.finance.importing.profiles import ProfileConfig, TimeUnit
from lararium.modules.finance.importing.staging import BatchStatus, RowKind, RowStatus
from lararium.modules.finance.instruments import IdentifierScheme, InstrumentKind
from lararium.modules.finance.ledger import TransactionOrigin, TransactionStatus
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.snapshots import SnapshotKind
from lararium.modules.finance.transfers import ReviewReason, SuggestedKind, TransferStatus
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
    # One the ledger keeps for itself (Unassigned, Opening balances, In transit).
    system: bool


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


# Imports


class ImportProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    config: ProfileConfig


class ImportProfileOut(_Out):
    id: UUID
    name: str
    config: ProfileConfig
    version: int
    updated_at: AwareDatetime


class ImportCatalogEntryOut(_Out):
    institution: str
    format: str
    label: str
    config: ProfileConfig
    recommended: bool


class ImportStatementRateOut(_Out):
    kind: str
    band_from: DecimalStr | None
    band_to: DecimalStr | None
    rate: DecimalStr | None
    ear: DecimalStr | None
    valid_from: date | None
    valid_to: date | None
    frequency: str | None


class ImportStatementOut(_Out):
    """What the file says at statement level; fields it doesn't print are null."""

    period_from: date | None
    period_to: date | None
    currency: str | None
    opening_balance: DecimalStr | None
    closing_balance: DecimalStr | None
    closing_balance_date: date | None
    money_in: DecimalStr | None
    money_out: DecimalStr | None
    interest: DecimalStr | None
    payments_in: int | None
    payments_out: int | None
    statement_reference: str | None
    document_date: date | None
    account_type: str | None
    account_name: str | None
    sort_code: str | None
    account_number: str | None
    iban: str | None
    bic: str | None
    customer_number: str | None
    clearing_account_number: str | None
    branch: str | None
    bank: str | None
    addressee: str | None
    address: str | None
    overdraft_limit: DecimalStr | None
    overdraft_limit_date: date | None
    available_balance: DecimalStr | None
    available_balance_date: date | None
    account_reference: str | None
    account_opened_on: date | None
    rates: list[ImportStatementRateOut]


class ImportBatchOut(_Out):
    id: UUID
    account_id: UUID
    profile_id: UUID
    profile_version: int
    document_id: UUID
    filename: str
    status: BatchStatus
    created_at: AwareDatetime
    committed_at: AwareDatetime | None
    rolled_back_at: AwareDatetime | None
    counts: dict[str, int]  # every row status, zeros included
    warnings: list[str]
    statement: ImportStatementOut | None


class ImportRowOut(_Out):
    id: UUID
    position: int
    kind: RowKind
    status: RowStatus
    booking_date: date | None
    occurred_at: AwareDatetime | None
    # What the file gave occurred_at to; "day": only a date, so it is that day's midnight.
    time_unit: TimeUnit | None
    # The row pays interest, so it is never a bank transfer.
    interest: bool
    value_date: date | None
    description: str
    payee: str | None
    memo: str | None
    check_number: str | None
    counterparty_name: str | None
    counterparty_sort_code: str | None
    counterparty_account_number: str | None
    counterparty_bank: str | None
    counterparty_account_ending: str | None
    payment_reference: str | None
    payment_kind: str | None
    bank_reference: str | None
    purpose_code: str | None
    remittance_info: str | None
    original_currency: str | None
    exchange_rate: DecimalStr | None
    started_at: AwareDatetime | None
    card_number: str | None
    original_amount: DecimalStr | None
    fx_rate: DecimalStr | None
    fx_reference_rate: DecimalStr | None
    quantity: DecimalStr | None
    fee: DecimalStr  # 0 when the file states none
    instrument_id: UUID | None
    balance: DecimalStr | None
    external_id: str | None
    bank_type: str | None
    error: str | None
    note: str | None
    raw: dict[str, str]
    transaction_id: UUID | None
    duplicate_of: UUID | None  # the committed row this one duplicates (Decision 16)
    # Adjustment rows: True when the adjustment follows earlier imported history (a likely
    # missing period), False when it is the account's opening; None on statement rows.
    is_gap: bool | None


class ImportBatchDetailOut(ImportBatchOut):
    rows: list[ImportRowOut]


# Transfers between own accounts, and the registry of the owner's account identifiers


class IdentifierCreate(BaseModel):
    # Only a UK bank account or a chain address is registered by hand; the connectors register
    # their own ids.
    kind: Literal["uk_bank_account", "chain_address"]
    value: str  # a sort code and account number, e.g. "11-22-33 12345678", or an address


class IdentifierOut(BaseModel):
    id: UUID
    kind: IdentifierKind
    value: str  # as stored: "<sort code>:<account number>" for a UK bank account
    sort_code: str | None  # a UK bank account's only
    account_number: str | None
    account_id: UUID | None
    # A card merchant's own accounts (it has no account_id); empty for every other kind.
    holder_account_ids: list[UUID]
    dismissed_at: AwareDatetime | None
    created_by: str
    created_at: AwareDatetime


class CardMerchantRegister(BaseModel):
    """The own accounts card payments to a Monzo merchant go into; none removes it."""

    merchant_id: str  # Monzo's merchant id, merch_…
    account_ids: list[UUID]


class CardMerchantDismiss(BaseModel):
    merchant_id: str


class BalanceModeUpdate(BaseModel):
    balance_mode: BalanceMode


class MainAccountUpdate(BaseModel):
    main_account_id: UUID | None


class ReviewOut(BaseModel):
    """A value two sources of one event disagree on (design §5.7): open, or resolved by the
    owner taking one of them."""

    id: UUID
    connection_id: UUID
    transaction_id: UUID | None
    event_key: str  # e.g. "close:<positionId>"
    # eToro: units | cash | profit | booking_date; Trading 212: amount | currency | booking_date
    field: str
    api_value: str
    statement_value: str
    opened_at: AwareDatetime
    resolved_at: AwareDatetime | None
    resolution: Literal["api", "statement"] | None
    resolved_by: str | None


class ReviewResolve(BaseModel):
    """Which source's value the owner takes for a review item."""

    take: Literal["api", "statement"]


class TransferOut(BaseModel):
    """A transfer as matching left it. Its id changes when matching reopens it, so a client
    finds it again by its own records' ids."""

    id: UUID
    from_account_id: UUID
    from_account_name: str
    to_account_id: UUID
    to_account_name: str
    instrument_id: UUID
    amount: DecimalStr  # positive, as sent
    currency: str  # the instrument's symbol
    status: TransferStatus
    review_reason: ReviewReason | None
    opened_at: AwareDatetime
    matched_at: AwareDatetime | None
    # Whole days since it was opened, in the owner's timezone; only for awaiting ones.
    days_in_transit: int | None
    overdue: bool
    out_transaction_id: UUID | None
    in_transaction_id: UUID | None
    placeholder_transaction_id: UUID | None
    # A matched pair a sort code alone made: the Transfers page offers Reject, not Unpair.
    weak: bool = False


class CandidateOut(BaseModel):
    transaction_id: UUID
    account_id: UUID
    account_name: str
    quantity: DecimalStr  # signed, as posted on the account
    currency: str  # its own instrument's symbol, which may differ from the transfer's
    occurred_at: AwareDatetime
    description: str
    reason: Literal["exact", "amount_differs", "unpaired"]


class PairCandidateOut(BaseModel):
    """A record the owner could pair with another as one transfer."""

    transaction_id: UUID
    account_id: UUID
    account_name: str
    quantity: DecimalStr  # signed, as posted on the account
    currency: str  # its own instrument's symbol, which may differ from the record's
    occurred_at: AwareDatetime
    booking_date: date
    description: str
    # How its side differs from the record's (two currencies), or None for one in the same
    # instrument of the opposite and equal amount.
    differs: Literal["currency", "amount"] | None = None


class TransferPair(BaseModel):
    # One of the transfer's candidates, or one unpaired from it; or, pairing two records,
    # the other record.
    transaction_id: UUID


class CardMerchantSuggestionOut(_Out):
    """A card merchant whose website is on a connected platform's domain, offering the
    platform's accounts; registering it is register_card_merchant."""

    kind: Literal["card_merchant"]
    merchant_id: str
    name: str | None
    host: str
    outgoing: int
    incoming: int
    latest: date
    account_ids: list[UUID]


class SuggestionOut(_Out):
    identifier_id: UUID
    kind: SuggestedKind  # never card_merchant: that is CardMerchantSuggestionOut
    value: str  # as stored
    sort_code: str | None  # a UK bank account's only
    account_number: str | None
    names: list[str]
    outgoing: int
    incoming: int
    latest: date
