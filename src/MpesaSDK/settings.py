"""Validated, immutable provider configuration.

Developers pass a plain ``dict`` (hyphenated or snake_case keys); it is turned into a
:class:`MpesaConfig` once, so a typo fails loudly at start-up rather than mid-payment.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from . import markets
from .errors import ConfigError
from .markets import Market


def _snake(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def _normalise(raw: Mapping[str, Any], allowed: set[str], where: str) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        name = _snake(str(key))
        if name not in allowed:
            valid = ", ".join(sorted(a.replace("_", "-") for a in allowed))
            raise ConfigError(f"Unknown {where} option {key!r}. Valid options: {valid}")
        cleaned[name] = value
    return cleaned


@dataclass(frozen=True, slots=True)
class RetryStrategy:
    """Exponential back-off: ``back_off * 2**(n-1)`` seconds before retry ``n``,
    capped at ``max_back_off``, plus a random ``0..jitter`` seconds."""

    retry_count: int = 3  # retries *after* the first attempt
    back_off: float = 5.0
    jitter: float = 2.0
    max_back_off: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.retry_count, int) or self.retry_count < 0:
            raise ConfigError("retry-count must be an integer >= 0")
        for name in ("back_off", "jitter", "max_back_off"):
            if getattr(self, name) < 0:
                raise ConfigError(f"{name.replace('_', '-')} must be >= 0")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RetryStrategy:
        allowed = {f.name for f in fields(cls)}
        return cls(**_normalise(raw, allowed, "retry-strategy"))


AuditHandler = Callable[[Any], Any]  # called with a TransactionResponse; may be async


@dataclass(frozen=True, slots=True)
class MpesaConfig:
    market: Market
    api_key: str = field(repr=False)
    public_key: str = field(repr=False)
    live: bool = False
    retry: RetryStrategy = field(default_factory=RetryStrategy)
    concurrency: bool = False
    max_workers: int = 4
    redis_url: str = "redis://localhost:6379/0"
    queue: str = "mpesa"
    celery_options: Mapping[str, Any] = field(default_factory=dict)
    audit_handler: AuditHandler | None = None
    request_timeout: float = 30.0
    session_ttl: float = 1800.0
    session_warmup: float = 0.0
    base_url: str = "https://openapi.m-pesa.com"
    origin: str = "*"

    def __post_init__(self) -> None:
        for name in ("api_key", "public_key"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigError(f"{name.replace('_', '-')} is required")
        if self.max_workers < 1:
            raise ConfigError("max-workers must be >= 1")
        if self.request_timeout <= 0:
            raise ConfigError("request-timeout must be > 0")
        if self.session_ttl <= 0 or self.session_warmup < 0:
            raise ConfigError("session-ttl must be > 0 and session-warmup >= 0")
        if self.audit_handler is not None and not callable(self.audit_handler):
            raise ConfigError("audit-handler must be callable")

    @property
    def environment(self) -> str:
        """URL segment: ``openapi`` for live, ``sandbox`` otherwise."""
        return "openapi" if self.live else "sandbox"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> MpesaConfig:
        allowed = {f.name for f in fields(cls)}
        data = _normalise(raw, allowed, "provider")
        missing = {"market", "api_key", "public_key"} - data.keys()
        if missing:
            raise ConfigError("Missing required option(s): " + ", ".join(sorted(missing)))
        data["market"] = markets.resolve(data["market"])
        retry = data.get("retry")
        if isinstance(retry, Mapping):
            data["retry"] = RetryStrategy.from_mapping(retry)
        elif retry is not None and not isinstance(retry, RetryStrategy):
            raise ConfigError("retry-strategy must be a mapping")
        return cls(**data)


def _alias_retry_strategy(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The public key is ``retry-strategy``; internally the field is ``retry``."""
    data = dict(raw)
    for key in list(data):
        if _snake(str(key)) == "retry_strategy":
            data["retry"] = data.pop(key)
    return data


def parse_config(raw: Mapping[str, Any] | MpesaConfig) -> MpesaConfig:
    if isinstance(raw, MpesaConfig):
        return raw
    return MpesaConfig.from_mapping(_alias_retry_strategy(raw))
