"""Channel-layout value type.

``ChannelLayout`` replaces free-text channel-layout strings everywhere a layout
is represented internally (audio metadata, extraction, downmix params/matrices,
bitrate scaling). It carries three things:

- ``original``   — the exact ffmpeg token as seen (``5.1(side)``, ``stereo``,
  ``7.1``), preserved verbatim for the faithful ``ch=`` conventional string and
  for any filter that cares about the qualifier.
- ``normalized`` — the canonical base layout (``5.1(side)`` → ``5.1``,
  ``stereo`` → ``2.0``), used for channel-count, downmix ``to:``/no-op
  comparison, matrix lookup, and bitrate scaling.
- ``channels``   — the derived channel count (``2.0``/``stereo`` = 2, ``5.1`` =
  6, ``7.1`` = 8), the basis for the ``<=`` downmix-only comparison.

Normalization rule of note: **``stereo`` normalizes to ``2.0``** — both are the
2-channel layout and sources frequently tag ``stereo``. Parsing accepts either
and yields the same normalized value, so a ``stereo`` source and a ``2.0`` source
compare equal.

The channel-count map for the known layouts lives in
:mod:`pyqenc.audio.matrices` (:func:`~pyqenc.audio.matrices.layout_channels`) and
is the single source of truth; it is reused here rather than duplicated. Layouts
outside that map (e.g. ``mono``, ``2.1``) fall back to a general count derived
from the dotted notation.
"""
# CHerSun 2026

from pydantic import BaseModel, ConfigDict

from pyqenc.audio.matrices import Layout, layout_channels

# Raw ffmpeg tokens that are aliases for a dotted normalized layout.
_ALIAS_NORMALIZED: dict[str, str] = {
    "stereo": Layout.STEREO.value,   # "2.0"
    "mono":   "1.0",
}


def _normalize_token(raw: str) -> str:
    """Return the canonical base layout for a raw ffmpeg channel-layout token.

    Strips any parenthesised qualifier (``5.1(side)`` → ``5.1``) and maps known
    aliases (``stereo`` → ``2.0``, ``mono`` → ``1.0``). Unknown tokens are
    returned trimmed and lower-cased so comparison is stable.

    Args:
        raw: A raw ffmpeg channel-layout token (e.g. ``5.1(side)``, ``stereo``).

    Returns:
        The normalized base layout token.
    """
    token = raw.strip().lower()
    # Strip a parenthesised qualifier, e.g. "5.1(side)" -> "5.1".
    if "(" in token:
        token = token.split("(", 1)[0].strip()
    return _ALIAS_NORMALIZED.get(token, token)


def _derive_channels(normalized: str) -> int:
    """Return the channel count for a normalized layout token.

    Uses :func:`~pyqenc.audio.matrices.layout_channels` (the single source of
    truth) for the known layouts (``2.0``, ``5.1``, ``7.1``). For any other
    normalized token it derives the count from the dotted notation
    (``main.lfe`` → ``main + lfe``, e.g. ``2.1`` → 3, ``6.1`` → 7), falling back
    to ``2`` (stereo) only when the token is not dotted-numeric.

    Args:
        normalized: A normalized layout token.

    Returns:
        The derived channel count.
    """
    try:
        return layout_channels(normalized)
    except KeyError:
        pass
    # General fallback: sum the dotted components (e.g. "2.1" -> 2 + 1 = 3).
    parts = normalized.split(".")
    try:
        return sum(int(part) for part in parts)
    except ValueError:
        return _derive_channels(Layout.STEREO.value)


class ChannelLayout(BaseModel):
    """A channel layout carrying both the faithful source token and its canonical form.

    ``ChannelLayout`` is the single internal representation of an audio track's
    channel layout. Construct it from a raw ffmpeg token via :meth:`parse`; the
    ``original`` token is preserved for the ``ch=`` conventional string while
    ``normalized`` / ``channels`` drive all internal layout decisions (matrix
    lookup, downmix no-op comparison, bitrate scaling).

    Attributes:
        original:   The exact ffmpeg token as seen (e.g. ``5.1(side)``,
                    ``stereo``, ``7.1``), preserved verbatim.
        normalized: The canonical base layout (e.g. ``5.1``, ``2.0``).
        channels:   The derived channel count.
    """

    model_config = ConfigDict(frozen=True)

    original:   str
    normalized: str
    channels:   int

    @classmethod
    def parse(cls, raw: str) -> "ChannelLayout":
        """Parse a raw ffmpeg channel-layout token into a ``ChannelLayout``.

        Normalization maps ``stereo`` → ``2.0`` and strips parenthesised
        qualifiers (``5.1(side)`` → ``5.1``). The original token is preserved
        verbatim.

        Args:
            raw: A raw ffmpeg channel-layout token (e.g. ``5.1(side)``,
                 ``stereo``, ``7.1``).

        Returns:
            A ``ChannelLayout`` with ``original``, ``normalized``, and
            ``channels`` populated.
        """
        original = raw.strip()
        normalized = _normalize_token(original)
        return cls(
            original   = original,
            normalized = normalized,
            channels   = _derive_channels(normalized),
        )

    def __str__(self) -> str:
        """Render as the faithful source token so filenames/tags show ``ch=<original>``."""
        return self.original
