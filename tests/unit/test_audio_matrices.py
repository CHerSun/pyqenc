"""Unit tests for downmix matrices and the layout channel-count map.

Each test names the concrete bug it guards against, per project standards, and
checks observable behaviour (the returned matrix string / channel count), never
internal representation.
"""

import pytest

from pyqenc.audio.matrices import (
    DOWNMIX_MATRICES,
    Layout,
    MatrixName,
    layout_channels,
)


class TestLayoutChannels:
    """`layout_channels()` maps normalized layouts to their channel counts."""

    def test_known_layouts_channel_counts(self) -> None:
        # Bug: a wrong channel count silently mis-scales bitrate and breaks the
        # downmix-only (<=) comparison, causing needless re-encodes or bad folds.
        assert layout_channels(Layout.STEREO.value) == 2
        assert layout_channels(Layout.SURROUND_51.value) == 6
        assert layout_channels(Layout.SURROUND_71.value) == 8

    def test_stereo_token_is_2_0(self) -> None:
        # Bug: `stereo` and `2.0` treated as different layouts would double-count
        # a stereo source as needing a downmix. Normalization collapses them to
        # `2.0`, which is the only 2-channel token this map knows.
        assert Layout.STEREO.value == "2.0"
        assert layout_channels("2.0") == 2

    def test_unknown_layout_raises(self) -> None:
        # Bug: a typo'd or unsupported layout silently returning a default count
        # would corrupt downstream scaling. It must fail loudly instead.
        with pytest.raises(KeyError):
            layout_channels("3.0")


class TestDownmixOnlyComparison:
    """Downmix is reduction-only: a fold applies iff source has MORE channels."""

    def test_downmix_needed_when_source_larger(self) -> None:
        # Bug: failing to downmix a 5.1 source to 2.0 leaves a 6-channel output
        # where stereo was requested.
        assert layout_channels(Layout.SURROUND_51.value) > layout_channels(Layout.STEREO.value)
        assert layout_channels(Layout.SURROUND_71.value) > layout_channels(Layout.SURROUND_51.value)

    def test_no_downmix_when_source_equal_or_smaller(self) -> None:
        # Bug: re-folding an already-stereo (or smaller) source needlessly
        # re-encodes and can alter the signal. Downmix is a no-op when
        # source channels <= target channels.
        assert layout_channels(Layout.STEREO.value) <= layout_channels(Layout.STEREO.value)
        assert layout_channels(Layout.STEREO.value) <= layout_channels(Layout.SURROUND_51.value)


class TestMatrixLookup:
    """`DOWNMIX_MATRICES` returns the expected, index-addressed pan spec."""

    def test_51_to_20_std_is_itu_lo_ro_lfe_dropped(self) -> None:
        # Bug: an LFE term sneaking into `std` would deviate from the canonical
        # ITU-R BS.775 / ATSC Lo/Ro fold (LFE must be dropped).
        spec = DOWNMIX_MATRICES[
            (Layout.SURROUND_51.value, Layout.STEREO.value, MatrixName.STD.value)
        ]
        assert spec == "pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5"
        assert "c3" not in spec  # LFE dropped

    def test_51_to_20_lfe_preserved_verbatim(self) -> None:
        # Bug: "improving" the historical night fold (e.g. bumping FC to 0.707 or
        # changing LFE gain) silently changes user-heard output. It is verbatim.
        spec = DOWNMIX_MATRICES[
            (Layout.SURROUND_51.value, Layout.STEREO.value, MatrixName.LFE.value)
        ]
        assert spec == (
            "pan=stereo|c0=0.5*c2+0.707*c0+0.707*c4+0.5*c3"
            "|c1=0.5*c2+0.707*c1+0.707*c5+0.5*c3"
        )

    def test_51_to_20_boosted_preserved_verbatim(self) -> None:
        # Bug: altering the nboost dialog-forward fold (full FC, 0.30 surrounds,
        # no LFE) changes the intended balance.
        spec = DOWNMIX_MATRICES[
            (Layout.SURROUND_51.value, Layout.STEREO.value, MatrixName.BOOSTED.value)
        ]
        assert spec == "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5"
        assert "c3" not in spec  # LFE dropped in boosted

    def test_71_to_51_is_plain_side_fold_no_matrix(self) -> None:
        # Bug: applying a matrix (or dropping/altering channels) on the 7.1→5.1
        # step; it must be a plain fold of the side pair (c6/c7) into the back
        # pair (c4/c5), keyed with a None matrix name.
        spec = DOWNMIX_MATRICES[
            (Layout.SURROUND_71.value, Layout.SURROUND_51.value, None)
        ]
        assert spec == "pan=5.1|c0=c0|c1=c1|c2=c2|c3=c3|c4=c4+c6|c5=c5+c7"

    def test_71_to_20_folds_side_pair_into_back_terms(self) -> None:
        # Bug: dropping the side channels (c6/c7) when going 7.1→2.0 loses the
        # surround content entirely. They must fold into the back terms.
        std = DOWNMIX_MATRICES[
            (Layout.SURROUND_71.value, Layout.STEREO.value, MatrixName.STD.value)
        ]
        assert "c6" in std and "c7" in std
        assert std == (
            "pan=stereo|c0=c0+0.707*c2+0.707*c4+0.707*c6"
            "|c1=c1+0.707*c2+0.707*c5+0.707*c7"
        )

    def test_all_matrices_are_index_addressed(self) -> None:
        # Bug: a channel-name-addressed matrix (FL=/FR=) can mis-map across ffmpeg
        # layout interpretations. Every matrix must address channels by index.
        for spec in DOWNMIX_MATRICES.values():
            assert spec.startswith("pan=")
            assert "FL=" not in spec and "FR=" not in spec

    def test_unknown_matrix_key_raises(self) -> None:
        # Bug: silently returning a default fold for an unregistered
        # (source, target, name) triple would produce wrong audio. Lookup must
        # fail loudly so callers surface a config error.
        with pytest.raises(KeyError):
            DOWNMIX_MATRICES[
                (Layout.SURROUND_51.value, Layout.STEREO.value, "nonexistent")
            ]
