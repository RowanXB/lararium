from decimal import Decimal
from uuid import UUID


class FinanceError(Exception):
    """Base class for finance module errors."""


class FinanceValidationError(FinanceError, ValueError):
    """The input breaks a finance rule: the caller has to change the request."""


class UnknownCurrencyError(FinanceError):
    """No instrument has the given ISO 4217 code."""


class NotACurrencyError(FinanceError):
    """The instrument exists but is not a fiat currency."""


class InstrumentNotFoundError(FinanceError):
    """No instrument has the given id."""


class DuplicateIdentifierError(FinanceError):
    """An identifier (scheme, value) already belongs to an instrument."""


class AccountNotFoundError(FinanceError):
    """No account has the given id."""


class AccountAlreadyClosedError(FinanceError):
    """The account is already closed."""


class TransactionNotFoundError(FinanceError):
    """No transaction has the given id."""


class LedgerImbalanceError(FinanceError):
    """Postings do not sum to zero per instrument; residuals maps instrument id to the sum."""

    def __init__(self, residuals: dict[UUID, Decimal]) -> None:
        super().__init__(f"ledger imbalance: {residuals}")
        self.residuals = residuals
