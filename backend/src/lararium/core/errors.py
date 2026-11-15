class SettingNotFoundError(Exception):
    """Raised when a key is missing from core.settings."""


class ConnectionNotFoundError(Exception):
    """Raised when no row in core.connections has the given id."""


class CredentialDecryptError(Exception):
    """Raised when a stored credential can't be decrypted: wrong key, wrong row, or tampering."""


class CredentialRejectedError(Exception):
    """Raised by a connector when the provider rejects the connection's credentials.

    The connection then needs the owner to authorise it again; retrying can't help.
    """


class DocumentNotFoundError(Exception):
    """Raised when no row in core.documents has the given id."""


class DocumentIntegrityError(Exception):
    """Raised when a document's stored file is missing or no longer matches its hash."""
