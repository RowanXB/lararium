"""What a parser reads from a statement file, before staging: shared by every format."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.importing.profiles import AmountFormat
from lararium.modules.finance.money import require_amount

MAX_IMPORT_ROWS = 20_000
MAX_DESCRIPTION_LENGTH = 1000

_CURRENCY_SYMBOLS = ("£", "$", "€")
_PLAIN_AMOUNT = re.compile(r"[0-9]+(\.[0-9]+)?")
_DIGITS = re.compile(r"[0-9]+")
# Written between digit groups, e.g. "12-34-56" or "1234 5678"; a leading "'" is how
# spreadsheets mark a cell as text.
_ACCOUNT_SEPARATORS = re.compile(r"[ -]")


def _digits(text: str, what: str) -> str:
    digits = _ACCOUNT_SEPARATORS.sub("", text.strip().removeprefix("'"))
    if not _DIGITS.fullmatch(digits):
        raise ValueError(f"not {what}: {text!r}")
    return digits


def normalise_sort_code(text: str | None) -> str | None:
    """Six digits with no separators."""
    if text is None:
        return None
    digits = _digits(text, "a sort code")
    if len(digits) != 6:
        raise ValueError(f"not a sort code: {text!r}")
    return digits


def normalise_account_number(text: str | None) -> str | None:
    """The account number's digits."""
    return None if text is None else _digits(text, "an account number")


@dataclass(frozen=True)
class Counterparty:
    """The other side of a payment, as the file states it (M4's counterparty details)."""

    name: str | None = None
    sort_code: str | None = None
    account_number: str | None = None
    reference: str | None = None
    bank: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sort_code", normalise_sort_code(self.sort_code))
        object.__setattr__(self, "account_number", normalise_account_number(self.account_number))


@dataclass(frozen=True)
class StatementAccount:
    """The account a statement is for, as the file states it."""

    sort_code: str | None = None
    account_number: str | None = None
    iban: str | None = None  # upper-cased, without spaces
    bic: str | None = None
    name: str | None = None
    account_type: str | None = None  # a product line, BOC's account type, or OFX ACCTTYPE
    customer_number: str | None = None
    clearing_account_number: str | None = None
    branch: str | None = None  # name and address as one text

    def __post_init__(self) -> None:
        object.__setattr__(self, "sort_code", normalise_sort_code(self.sort_code))
        object.__setattr__(self, "account_number", normalise_account_number(self.account_number))
        if self.iban is not None:
            object.__setattr__(self, "iban", "".join(self.iban.split()).upper())


@dataclass(frozen=True)
class StatementRate:
    """One row of a statement's rate table, in percent a year: the rate as printed (AER or
    nominal) and the EAR, at least one of them."""

    kind: str  # as printed
    band_from: Decimal | None
    band_to: Decimal | None
    rate: Decimal | None
    ear: Decimal | None

    def __post_init__(self) -> None:
        if self.rate is None and self.ear is None:
            raise ValueError("a rate row needs a rate or an EAR")


@dataclass(frozen=True)
class ParsedRow:
    """One transaction. quantity is in ledger sign (inflow positive); the account's balance
    changes by quantity - fee. occurred_at is aware when the file gives a time, else None."""

    position: int
    booking_date: date
    occurred_at: datetime | None
    description: str
    quantity: Decimal
    fee: Decimal
    currency: str | None
    balance: Decimal | None
    external_id: str | None
    bank_type: str | None
    raw: dict[str, str]
    value_date: date | None = None
    payee: str | None = None
    memo: str | None = None
    check_number: str | None = None
    counterparty: Counterparty | None = None
    payment_kind: str | None = None
    bank_reference: str | None = None
    purpose_code: str | None = None
    remittance_info: str | None = None


@dataclass(frozen=True)
class RowError:
    position: int
    message: str
    raw: dict[str, str]


@dataclass(frozen=True)
class SkippedRow:
    """A row the profile's include_only filter leaves out."""

    position: int
    reason: str
    raw: dict[str, str]


@dataclass(frozen=True)
class ParsedStatement:
    rows: list[ParsedRow | RowError | SkippedRow]
    closing_balance: Decimal | None = None
    closing_balance_date: date | None = None
    period: tuple[date, date] | None = None
    opening_balance: Decimal | None = None
    money_in: Decimal | None = None
    money_out: Decimal | None = None
    payments_in: int | None = None
    payments_out: int | None = None
    account: StatementAccount | None = None
    currency: str | None = None
    statement_reference: str | None = None
    document_date: date | None = None
    available_balance: Decimal | None = None
    available_balance_date: date | None = None
    overdraft_limit: Decimal | None = None
    overdraft_limit_date: date | None = None
    rates: list[StatementRate] = field(default_factory=list[StatementRate])


def parse_amount(text: str, fmt: AmountFormat) -> Decimal:
    """An amount as a bank writes it: "-£1,234.50", "(12.00)", "+85"; ValueError otherwise."""
    body = text.strip()
    negative = False
    if body.startswith("(") and body.endswith(")"):
        negative = True
        body = body[1:-1]
    elif body[:1] in ("+", "-"):
        negative = body[0] == "-"
        body = body[1:]
    if body.startswith(_CURRENCY_SYMBOLS):
        body = body[1:]
    separator = fmt.thousands_separator
    if separator and separator in body:
        # Only real grouping: "12,50" with "," for thousands is an error, never 1250.
        whole, _, fraction = body.partition(fmt.decimal_separator)
        grouped = rf"[0-9]{{1,3}}(?:{re.escape(separator)}[0-9]{{3}})+"
        if separator in fraction or not re.fullmatch(grouped, whole):
            raise ValueError(
                f"not an amount: {text!r} (thousands must be grouped in threes by {separator!r})"
            )
        body = body.replace(separator, "")
    if fmt.decimal_separator != ".":
        body = body.replace(fmt.decimal_separator, ".")
    if not _PLAIN_AMOUNT.fullmatch(body):
        raise ValueError(f"not an amount: {text!r}")
    value = Decimal(body)
    try:
        require_amount("an amount", value)
    except FinanceValidationError as error:
        raise ValueError(f"not an amount: {text!r} ({error})") from None
    # 0 - value, not -value: keeps "(0.00)" from becoming -0.00.
    return Decimal(0) - value if negative else value


def keyed(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """The pairs as a raw dict, in order, losing none: a repeated key gets "#2", "#3"…, and
    a numbered key that is already taken is numbered again. Each key resumes from its own
    counter, so a file of repeated fields stays linear."""
    values: dict[str, str] = {}
    seen: dict[str, int] = {}
    for key, value in pairs:
        n = seen.get(key, 1)
        candidate = key
        while candidate in values:
            n += 1
            candidate = f"{key}#{n}"
        seen[key] = n
        values[candidate] = value
    return values


def clean_description(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())[:MAX_DESCRIPTION_LENGTH]
