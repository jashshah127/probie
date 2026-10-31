"""Money helpers. Every amount in Probie is an integer number of cents."""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Plain digits ("1234.56") or US thousands separators ("1,234.56"), with an optional "$".
_PLAIN = re.compile(r"^\$?(\d+)(\.\d+)?$")
_GROUPED = re.compile(r"^\$?(\d{1,3}(?:,\d{3})+)(\.\d+)?$")


def fmt(cents: int) -> str:
    """Format cents as dollars: 123456 -> "$1,234.56"."""
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def parse_dollars(value: object) -> int | None:
    """Turn a dollar amount from a model into cents.

    Accepts numbers and strings such as "1234.5", "$1,234.56" or "1234.56 USD".
    Returns None for anything that isn't a finite, non-negative amount.
    Fractions of a cent are rounded half up.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value * 100 if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        amount = Decimal(repr(value))
    elif isinstance(value, str):
        text = value.strip()
        for affix in ("USD", "usd"):
            if text.endswith(affix):
                text = text[: -len(affix)].strip()
            if text.startswith(affix):
                text = text[len(affix):].strip()
        text = text.replace(" ", "")
        if _PLAIN.match(text):
            digits = text.lstrip("$")
        elif _GROUPED.match(text):
            digits = text.lstrip("$").replace(",", "")
        else:
            return None
        try:
            amount = Decimal(digits)
        except InvalidOperation:
            return None
    else:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
