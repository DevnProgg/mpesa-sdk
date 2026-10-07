"""Supported M-Pesa markets (values taken from the OpenAPI "Markets" table)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ConfigError

# The documented MSISDN rule: 12-14 digits.
_DOCUMENTED_MSISDN = re.compile(r"^\d{12,14}$")


@dataclass(frozen=True, slots=True)
class Market:
    """One M-Pesa market: URL context plus the country/currency sent in each request."""

    name: str
    context: str  # the [market] segment of the endpoint URL
    country: str  # input_Country
    currency: str  # input_Currency
    msisdn_pattern: re.Pattern[str] = _DOCUMENTED_MSISDN

    def is_valid_msisdn(self, value: str) -> bool:
        return bool(self.msisdn_pattern.match(value))

    def __str__(self) -> str:
        return self.name


GHANA = Market("GHANA", "vodafoneGHA", "GHA", "GHS")
TANZANIA = Market("TANZANIA", "vodacomTZN", "TZN", "TZS")
# Lesotho numbers are 266 + 8 digits = 11 digits, which the documented 12-14 digit
# rule would reject. Verify against the sandbox; this is the one place to change it.
LESOTHO = Market("LESOTHO", "vodacomLES", "LES", "LSL", re.compile(r"^\d{11,14}$"))
DR_CONGO = Market("DR_CONGO", "vodacomDRC", "DRC", "USD")
MOZAMBIQUE = Market("MOZAMBIQUE", "vodacomMOZ", "MOZ", "MZN")

ALL: tuple[Market, ...] = (GHANA, TANZANIA, LESOTHO, DR_CONGO, MOZAMBIQUE)


def resolve(value: Market | str) -> Market:
    """Accept a :class:`Market` or its name / URL context / country code."""
    if isinstance(value, Market):
        return value
    needle = str(value).strip().lower().replace(" ", "_")
    for market in ALL:
        if needle in {market.name.lower(), market.context.lower(), market.country.lower()}:
            return market
    valid = ", ".join(m.name for m in ALL)
    raise ConfigError(f"Unknown market {value!r}. Valid markets: {valid}")
