"""Exception hierarchy. Everything the SDK raises derives from :class:`MpesaError`."""

from __future__ import annotations

from collections.abc import Mapping


class MpesaError(Exception):
    """Base class for every error raised by the SDK."""


class ConfigError(MpesaError):
    """The provider configuration is invalid or incomplete."""


class CryptoError(MpesaError):
    """The API key / session key could not be encrypted (usually a bad public key)."""


class ValidationError(MpesaError):
    """A transaction payload failed validation. ``errors`` maps field -> message."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(
            "; ".join(f"{field}: {message}" for field, message in self.errors.items())
        )


class SessionError(MpesaError):
    """A session key could not be obtained from M-Pesa."""

    def __init__(self, message: str, response: object | None = None) -> None:
        super().__init__(message)
        self.response = response


class TransactionError(MpesaError):
    """Raised by callers when a transaction response is not ``ok``.

    >>> if not response.ok:
    ...     raise TransactionError(response.err.message)
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
