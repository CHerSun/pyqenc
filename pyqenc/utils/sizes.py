"""User-facing size parsing — loose string in, exact byte count out."""
# CHerSun 2026

import re
from typing import Annotated

from pydantic import BeforeValidator

_SIZE_PATTERN = re.compile(r"^\s*(\d+)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
"""Digits plus an optional single-letter unit: ``2G``, ``2000M``, ``1 GiB``, ``512 K``."""

_MULTIPLIERS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
"""Binary multiples (``k`` = 1024) — the same convention ``fmt_size_mb`` renders with."""


def parse_size(value: int | str) -> int:
    """Parse a user-facing size into its exact byte count.

    Loose in: an integer plus an optional ``k``/``m``/``g``/``t`` unit
    (case-insensitive, optionally ``iB``/``B``-suffixed, whitespace around
    the unit tolerated — ``"2G"``, ``"2000M"``, ``"1 GiB"``). A bare integer
    is bytes. Exact out: the byte count as ``int``.

    Fractional values (``"1.5G"``, ``1.0``) are refused — the user states a
    whole number of units or nothing; pyqenc never rounds a size for them.

    Args:
        value: The raw config value (yaml string or already-numeric).

    Returns:
        The size in bytes.

    Raises:
        ValueError: On anything unparsable — empty or non-numeric input,
            fractional numbers, negative values, or an unknown unit.
    """
    if isinstance(value, str):
        match = _SIZE_PATTERN.match(value)
        if match is None:
            raise ValueError(
                f"cannot parse size from {value!r} — expected digits plus an "
                f"optional k/m/g/t unit (e.g. '2G', '2000M', '1 GiB')"
            )
        number, unit = match.groups()
        return int(number) * _MULTIPLIERS[unit.lower()]
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError(f"negative size: {value}")
        return value
    raise ValueError(
        f"cannot parse size from {value!r} — expected digits plus an optional "
        f"k/m/g/t unit (e.g. '2G', '2000M', '1 GiB')"
    )


SizeYaml = Annotated[int, BeforeValidator(parse_size)]
"""A byte count accepted loosely from yaml (``1 GiB`` / ``2G`` / bare int) —
exact ``int`` bytes in memory."""
