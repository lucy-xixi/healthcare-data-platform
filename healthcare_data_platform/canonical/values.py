"""Source-value helpers that do not rewrite clinical meaning."""

from __future__ import annotations

import re

_DECIMAL = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def finite_decimal(raw_text: str) -> str | None:
    """Return the original numeric text when it is an unambiguous decimal.

    Empty text, spaces, thousands separators, and scientific notation stay
    non-numeric. The returned text is the source text, not a reformatted number.
    """

    if _DECIMAL.fullmatch(raw_text):
        return raw_text
    return None
