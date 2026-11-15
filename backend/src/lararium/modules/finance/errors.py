from decimal import Decimal
from uuid import UUID


class FinanceError(Exception):
    """Base class for finance module errors.

    Raised with the subject (an id, a code), it reads as a sentence about it: the API sends
    the message to clients, and the web UI shows it as is.
    """

    template = "{}"

    def __init__(self, subject: str) -> None:
        super().__init__(self.template.format(subject))


class FinanceValidationError(FinanceError, ValueError):
    """The input breaks a finance rule: the caller has to change the request.

    Raised with the whole message, which says which rule.
    """


class UnknownCurrencyError(FinanceError):
    """No instrument has the given ISO 4217 code."""

    template = "unknown currency: {}"


class NotACurrencyError(FinanceError):
    """The instrument exists but is not a fiat currency."""

    template = "not a fiat currency: {}"


class InstrumentNotFoundError(FinanceError):
    """No instrument has the given id."""

    template = "no instrument has the id {}"


class DuplicateIdentifierError(FinanceError):
    """An identifier (scheme, value) already belongs to an instrument."""

    template = "another instrument already has the identifier {}"


class AccountNotFoundError(FinanceError):
    """No account has the given id."""

    template = "no account has the id {}"


class AccountAlreadyClosedError(FinanceError):
    """The account is already closed."""

    template = "the account {} is already closed"


class TransactionNotFoundError(FinanceError):
    """No transaction has the given id."""

    template = "no transaction has the id {}"


class ImportProfileNotFoundError(FinanceError):
    """No import profile has the given id."""

    template = "no import profile has the id {}"


class DuplicateProfileNameError(FinanceError):
    """Another import profile already has the name (compared case-insensitively)."""

    template = "another import profile is already named {}"


class FileFormatError(FinanceError):
    """An imported file can't be read as a whole (wrong encoding, missing columns, no rows).

    Raised with the whole message, which says what is wrong with the file.
    """


class LedgerImbalanceError(FinanceError):
    """Postings do not sum to zero per instrument; residuals maps instrument id to the sum."""

    def __init__(self, residuals: dict[UUID, Decimal]) -> None:
        super().__init__("the postings don't balance per instrument")
        self.residuals = residuals
