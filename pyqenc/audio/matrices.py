"""Built-in downmix pan matrices and layout channel-count map.

Downmix matrices reduce a source channel layout to a smaller target layout. They
are looked up by the normalized ``(source_layout, target_layout, matrix_name)``
triple (see :data:`DOWNMIX_MATRICES`) and returned as ready-to-use ffmpeg ``pan``
filter fragments.

Design decisions (see ``.kiro/specs/audio-chains/design.md``):

- **Index-addressed, never channel-name-addressed.** Every matrix uses
  ``pan=<layout>|c0=…|c1=…`` addressing channels by physical position. Named
  addressing (``FL=…``) depends on ffmpeg's interpretation of the input layout,
  which varies across 5.1 encodings (``5.1`` vs ``5.1(side)``, back vs side
  labels) and can silently mis-map. Index addressing takes channels by position
  and is robust.
- **Canonical channel order** is assumed for all matrices::

      5.1: c0=FL c1=FR c2=FC c3=LFE c4=BL c5=BR
      7.1: c0=FL c1=FR c2=FC c3=LFE c4=BL c5=BR c6=SL c7=SR

- **Open registry.** ``DOWNMIX_MATRICES`` is a plain table; adding a named matrix
  is a single new entry and requires no change to the downmix filter or the chain
  executor.

Matrix flavours for the 5.1→2.0 (and derived 7.1→2.0) fold:

- ``std``     — canonical ITU-R BS.775 / ATSC Lo/Ro fold, LFE (c3) dropped.
- ``lfe``     — the historical "night" community fold: mixes FC, surrounds, and a
                share of the LFE into both channels. Preserved verbatim.
- ``boosted`` — the historical "nboost" community fold: dialog-forward (full FC,
                reduced surrounds, LFE dropped). Preserved verbatim.

``lfe`` and ``boosted`` are community-sourced formulas kept verbatim as distinct
named matrices; they differ in FC weight, surround weight, and LFE handling — not
merely in an LFE gain — which is why they are separate entries rather than one
parameterized fold.
"""
# CHerSun 2026

from enum import StrEnum


class Layout(StrEnum):
    """Normalized channel-layout tokens used for matrix lookup and comparison.

    These are the *normalized* forms only (``stereo`` normalizes to ``STEREO``'s
    value ``2.0`` upstream in the :class:`~pyqenc.audio.layout.ChannelLayout`
    parser — Task 4). Matrix keys and the channel-count map use these values.
    """

    STEREO = "2.0"
    SURROUND_51 = "5.1"
    SURROUND_71 = "7.1"


class MatrixName(StrEnum):
    """Built-in downmix matrix names selectable via a ``downmix`` filter's ``matrix:``."""

    STD = "std"
    LFE = "lfe"
    BOOSTED = "boosted"


_LAYOUT_CHANNELS: dict[str, int] = {
    Layout.STEREO.value: 2,
    Layout.SURROUND_51.value: 6,
    Layout.SURROUND_71.value: 8,
}
"""Normalized layout token → channel count. ``stereo`` is normalized to ``2.0``
upstream, so only ``2.0`` appears here."""


def layout_channels(normalized_layout: str) -> int:
    """Return the channel count for a normalized layout token.

    Args:
        normalized_layout: A normalized layout token (e.g. ``2.0``, ``5.1``,
            ``7.1``). ``stereo`` is expected to already be normalized to ``2.0``.

    Returns:
        The number of channels for the layout.

    Raises:
        KeyError: When the layout token is not a known normalized layout.
    """
    return _LAYOUT_CHANNELS[normalized_layout]


DOWNMIX_FORMAT: str ="aformat=sample_fmts=flt"
"""ffmpeg filter fragment to convert the input to float samples for downmixing. This is needed because the built-in downmix matrices
can produce positive gain, which cannot be represented in 16-bit/32-bit integer samples. The `flt` (and `dbl`) format is used to avoid clipping
and preserve audio fidelity during the downmixing process."""

DOWNMIX_MATRICES: dict[tuple[str, str, str | None], str] = {
    #@ ___ 7.1 → 5.1 ___
    # Plain index fold of the side pair into the back pair, no matrix.
    (Layout.SURROUND_71.value, Layout.SURROUND_51.value, None):
        "pan=5.1|c0=c0|c1=c1|c2=c2|c3=c3|c4=0.5*c6+0.5*c4|c5=0.5*c7+0.5*c5",       # Direct folding of channels.
    #@ ___ 5.1 → 2.0 ___
    (Layout.SURROUND_51.value, Layout.STEREO.value, MatrixName.STD.value):
        "pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5",                                         # std: ITU-R BS.775 / ATSC Lo/Ro fold, LFE (c3) dropped.
    (Layout.SURROUND_51.value, Layout.STEREO.value, MatrixName.LFE.value):
        "pan=stereo|c0=0.3431*c0+0.2426*c2+0.2426*c4+0.1716*c3|c1=0.3431*c1+0.2426*c2+0.2426*c5+0.1716*c3",   # Standard Dolby Downmix with LFE adjusted for power
        #"pan=stereo|c0=0.5*c2+0.707*c0+0.707*c4+0.5*c3|c1=0.5*c2+0.707*c1+0.707*c5+0.5*c3",                  # David's LFE downmix from doom9 forum
    (Layout.SURROUND_51.value, Layout.STEREO.value, MatrixName.BOOSTED.value):
        "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5",                                             # Boosted dialogs from doom9 forum.
    #@ ___ 7.1 → 2.0 ___
    (Layout.SURROUND_71.value, Layout.STEREO.value, MatrixName.STD.value):
        "pan=stereo|c0=c0+0.707*c2+0.707*c4+0.707*c6|c1=c1+0.707*c2+0.707*c5+0.707*c7",        # std: ITU-R BS.775 / ATSC Lo/Ro fold, LFE (c3) dropped.
    (Layout.SURROUND_71.value, Layout.STEREO.value, MatrixName.LFE.value):
        "pan=stereo|c0=0.2761*c0+0.1953*c2+0.1953*c6+0.1953*c4+0.1381*c3"
                  "|c1=0.2761*c1+0.1953*c2+0.1953*c7+0.1953*c5+0.1381*c3",                     # Standard Dolby Downmix with LFE adjusted for power
        #"pan=stereo|c0=0.5*c2+0.707*c0+0.707*c4+0.707*c6+0.5*c3"
        #"|c1=0.5*c2+0.707*c1+0.707*c5+0.707*c7+0.5*c3",                                       # David's LFE downmix
    # boosted: FC full, surrounds (back+side) 0.30, LFE dropped.
    (Layout.SURROUND_71.value, Layout.STEREO.value, MatrixName.BOOSTED.value):
        "pan=stereo|c0=c2+0.30*c0+0.30*c4+0.30*c6|c1=c2+0.30*c1+0.30*c5+0.30*c7",
}
"""Registry of built-in downmix matrices, index-addressed.

Keyed by ``(normalized source layout, normalized target layout, matrix name)``.
The 7.1→5.1 fold takes no matrix, so its key's matrix-name slot is ``None``. The
set is open — add a named matrix by adding an entry; no filter/executor change is
needed."""
