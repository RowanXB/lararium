class SettingNotFoundError(Exception):
    """Raised when a key is missing from core.settings."""


class ConnectionNotFoundError(Exception):
    """Raised when no row in core.connections has the given id."""


class CredentialDecryptError(Exception):
    """Raised when a stored credential can't be decrypted: wrong key, wrong row, or tampering."""
