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


class DuplicateAccountIdentifierError(FinanceError):
    """An account identifier (kind, value) already belongs to an account, given by name."""

    template = "this identifier already belongs to the account {}"


class AccountIdentifierNotFoundError(FinanceError):
    """No account identifier has the given id."""

    template = "no account identifier has the id {}"


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


class ImportNotAllowedError(FinanceError):
    """The account can't take this import (closed, synced from a connection, valued by
    snapshots, or a file for another bank account).

    Raised with the whole message, which says why.
    """


class TransactionImportedError(FinanceError):
    """The transaction was posted by a committed import, so voiding it by hand would leave
    the import's rows saying it stands (and its openings unaligned)."""

    template = "this transaction was imported from {}: roll that import back instead"


class ImportBatchNotFoundError(FinanceError):
    """No import batch has the given id."""

    template = "no import batch has the id {}"


class ImportBatchStateError(FinanceError):
    """The batch's status doesn't allow the action (e.g. committing a committed batch), or
    another batch relies on it.

    Raised with the whole message, which says why.
    """


class ImportHasErrorsError(FinanceError):
    """The batch has error rows, so it can't be committed."""

    template = (
        "the import batch {} has rows with errors; fix the file or the profile and upload it again"
    )


class ImportBatchStaleError(FinanceError):
    """The ledger moved since the batch was staged (Decision 13 or 16): upload it again.

    Raised with the whole message.
    """


class FileFormatError(FinanceError):
    """An imported file can't be read as a whole (wrong encoding, missing columns, no rows).

    Raised with the whole message, which says what is wrong with the file.
    """


class TransferNotFoundError(FinanceError):
    """No transfer has the given id."""

    template = "no transfer has the id {}"


class TransferSettingsError(FinanceError, ValueError):
    """The setting finance.transfers is malformed, so transfers can't be matched until the
    owner fixes it. Raised with the whole message, which says what is wrong."""


class LedgerImbalanceError(FinanceError):
    """Postings do not sum to zero per instrument; residuals maps instrument id to the sum."""

    def __init__(self, residuals: dict[UUID, Decimal]) -> None:
        super().__init__("the postings don't balance per instrument")
        self.residuals = residuals
