"""Shared filesystem-name primitives for the naming-ownership rules.

Exactly two kinds of names exist in the pipeline. **Display names** carry any
symbols and never touch the filesystem. **Filesystem names** are constrained,
and this module holds the single shared pair of primitives every naming family
builds on:

- config-sourced names (profiles, presets, chains) are **checked** — unsafe
  characters are a configuration error rejected at load, naming the offender;
- media-sourced free text (stream titles above all) is **sanitized** —
  replacement, never rejection, so a stream with an arbitrary title is always
  consumable.
"""
# CHerSun 2026

from pyqenc.constants import (
    FILENAME_CONTROL_CHARS,
    FILENAME_FORBIDDEN_CHARS,
    FILENAME_SANITIZATION_REPLACEMENT,
)

_UNSAFE_CHARS: frozenset[str] = FILENAME_FORBIDDEN_CHARS | FILENAME_CONTROL_CHARS
"""The complete unsafe set: Windows-forbidden characters plus control chars."""


def is_filesystem_safe_name(name: str) -> bool:
    """Return ``True`` when ``name`` carries no filesystem-unsafe characters.

    Used at config load for names that are embedded verbatim in filesystem
    paths (profiles, presets, chains): an unsafe name is rejected there, so no
    sanitization ever executes in a naming path.

    Args:
        name: The candidate name.

    Returns:
        ``True`` when every character is filesystem-safe.
    """
    return not (set(name) & _UNSAFE_CHARS)


def sanitize_filesystem_text(text: str) -> str:
    """Replace every filesystem-unsafe character in media-sourced free text.

    The sanitize primitive for **media-sourced free text only** (stream titles
    above all): Windows-forbidden characters and control chars are replaced
    with :data:`FILENAME_SANITIZATION_REPLACEMENT` — replacement, never
    rejection, so the pipeline never fails a stream because of its title.

    Args:
        text: Free text destined for a filesystem name.

    Returns:
        The text with every unsafe character replaced.
    """
    return "".join(
        FILENAME_SANITIZATION_REPLACEMENT if ch in _UNSAFE_CHARS else ch
        for ch in text
    )
