"""Tests for the output-scale types and resolution math."""
# CHerSun 2026

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pyqenc.models import (
    CropParams,
    Resolution,
    ResolvedScale,
    ScaleAlgorithm,
    ScaleMode,
    ScaleSpec,
    comparison_scale,
    resolve_scale,
)

ALG = ScaleAlgorithm.LANCZOS


def _spec(target: str, mode: ScaleMode = ScaleMode.CONTAIN) -> ScaleSpec:
    """Build a spec with the test algorithm."""
    return ScaleSpec.parse(target, mode=mode, algorithm=ALG)


class TestScaleSpec:
    """ScaleSpec parsing and invariants."""

    def test_happy_path_parse_forms(self) -> None:
        """W and WxH targets parse into the spec."""
        assert _spec("1920") == ScaleSpec(mode=ScaleMode.CONTAIN, width=1920, height=None, algorithm=ALG)
        exact = _spec("1920x800", ScaleMode.EXACT)
        assert (exact.width, exact.height, exact.mode) == (1920, 800, ScaleMode.EXACT)

    def test_error_handling_parse_rejects_bad_targets(self) -> None:
        """Non W/WxH forms raise with a clear message."""
        for bad in ("", "x1080", "1920x", "-5", "1920x-4", "a xb", "1920x1080x720"):
            with pytest.raises(ValueError, match="Invalid --scale target"):
                _spec(bad)

    def test_error_handling_mode_requires_height(self) -> None:
        """Cover and exact need an explicit height; contain admits the W-only box."""
        with pytest.raises(ValueError, match="requires an explicit height"):
            _spec("1920", ScaleMode.COVER)
        with pytest.raises(ValueError, match="requires an explicit height"):
            _spec("1920", ScaleMode.EXACT)

    def test_error_handling_off_and_unknown_algorithm(self) -> None:
        """OFF never appears in a spec; the algorithm is the closed swscale
        set — an unknown name fails validation listing the valid ones."""
        with pytest.raises(ValueError, match="never encodes absence"):
            ScaleSpec(mode=ScaleMode.OFF, width=1920, height=None, algorithm=ALG)
        with pytest.raises(Exception, match="'fast_bilinear', 'bilinear', 'bicubic'"):
            ScaleSpec(mode=ScaleMode.CONTAIN, width=1920, height=None, algorithm="not-a-scaler")


class TestResolvedScale:
    """ResolvedScale invariants, storage round-trip, and filter segments."""

    def test_happy_path_storage_round_trip(self) -> None:
        """The storage string parses back to an equal value."""
        for value in (
            ResolvedScale(),
            ResolvedScale(mode=ScaleMode.CONTAIN, width=1920, height=800, algorithm=ALG),
            ResolvedScale(mode=ScaleMode.EXACT, width=720, height=576, algorithm=ScaleAlgorithm.BICUBIC),
        ):
            assert ResolvedScale.parse(str(value)) == value

    def test_error_handling_mixed_states_rejected(self) -> None:
        """OFF with dimensions and applied without algorithm both fail validation."""
        with pytest.raises(ValueError, match="OFF carries no dimensions"):
            ResolvedScale(mode=ScaleMode.OFF, width=1920, height=800, algorithm=ALG)
        with pytest.raises(ValueError, match="requires positive dimensions"):
            ResolvedScale(mode=ScaleMode.CONTAIN, width=1920, height=800, algorithm=None)

    def test_happy_path_filter_segments(self) -> None:
        """Off is no segment; applied modes emit scale (plus setsar under exact)."""
        assert ResolvedScale().to_ffmpeg_filter() == ""
        assert ResolvedScale(mode=ScaleMode.CONTAIN, width=1920, height=800, algorithm=ALG).to_ffmpeg_filter() == (
            "scale=1920:800:flags=lanczos"
        )
        assert ResolvedScale(mode=ScaleMode.EXACT, width=1920, height=800, algorithm=ALG).to_ffmpeg_filter() == (
            "scale=1920:800:flags=lanczos,setsar=1"
        )


class TestResolveScale:
    """The DAR-aware resolution math."""

    def test_happy_path_contain_width_only(self) -> None:
        """The canonical archive shrink: 4k scope source into a 1920 box."""
        resolved = resolve_scale(Resolution.parse("3840x1600"), None, CropParams(), _spec("1920"), 2)
        assert (resolved.mode, resolved.width, resolved.height) == (ScaleMode.CONTAIN, 1920, 800)

    def test_happy_path_anamorphic_fits_displayed_aspect(self) -> None:
        """The fit is DAR-driven: 720x576 SAR 16:15 (DAR 4:3) into 640x576
        lands on 640x480 — the storage-ratio fit (ffmpeg's
        force_original_aspect_ratio semantics) would wrongly give 640x512."""
        resolved = resolve_scale(
            Resolution.parse("720x576"), Fraction(16, 15), CropParams(), _spec("640x576"), 2,
        )
        assert (resolved.width, resolved.height) == (640, 480)

    def test_happy_path_cover_at_minimum_covering_size_is_noop(self) -> None:
        """The source already sits at the minimum covering size — no scaling."""
        resolved = resolve_scale(
            Resolution.parse("720x576"), Fraction(16, 15), CropParams(), _spec("640x576", ScaleMode.COVER), 2,
        )
        assert resolved == ResolvedScale()

    def test_happy_path_cover_grows_to_reach_box(self) -> None:
        """Cover of a box below the source on both axes scales down to the
        minimum size that covers it (the fill-the-frame case contain would
        letterbox)."""
        resolved = resolve_scale(
            Resolution.parse("3840x1600"), None, CropParams(), _spec("1920x1080", ScaleMode.COVER), 2,
        )
        assert (resolved.width, resolved.height) == (2592, 1080)

    def test_happy_path_cover_never_upscales(self) -> None:
        """A cover box the source cannot reach without upscaling is a silent
        no-op — never-upscale trumps cover, like contain's already-fits no-op."""
        resolved = resolve_scale(
            Resolution.parse("720x576"), Fraction(16, 15), CropParams(),
            _spec("800x600", ScaleMode.COVER), 2,
        )
        assert resolved == ResolvedScale()

    def test_error_handling_quantization_jump_past_source(self) -> None:
        """A coarse granularity can quantize the covering height past the
        source's own height — that is a displayed upscale; loud error."""
        with pytest.raises(ValueError, match="never\\s+upscales"):
            resolve_scale(
                Resolution.parse("18x18"), None, CropParams(),
                _spec("17x17", ScaleMode.COVER), 16,
            )

    def test_error_handling_below_granularity(self) -> None:
        """Targets below one granularity unit admit no on-grid result."""
        with pytest.raises(ValueError, match="below the codec"):
            resolve_scale(Resolution.parse("1920x1080"), None, CropParams(), _spec("8", ScaleMode.CONTAIN), 16)

    def test_happy_path_exact_snaps_sar(self) -> None:
        """Exact at the storage dims of an anamorphic source still applies —
        the setsar=1 redefinition is the point of the mode."""
        resolved = resolve_scale(
            Resolution.parse("720x576"), Fraction(16, 15), CropParams(), _spec("720x576", ScaleMode.EXACT), 2,
        )
        assert (resolved.mode, resolved.width, resolved.height) == (ScaleMode.EXACT, 720, 576)
        assert "setsar=1" in resolved.to_ffmpeg_filter()

    def test_happy_path_noop_targets_resolve_off(self) -> None:
        """No request, an already-fitting contain target, and exact-at-native
        on a square source all resolve to OFF — pixel-identical products key
        identically, so no-op targets never invalidate."""
        square = Resolution.parse("1280x720")
        assert resolve_scale(square, None, CropParams(), None, 2) == ResolvedScale()
        assert resolve_scale(square, None, CropParams(), _spec("1920"), 2) == ResolvedScale()
        exact_native = resolve_scale(
            Resolution.parse("3840x1600"), None, CropParams(), _spec("3840x1600", ScaleMode.EXACT), 2,
        )
        assert exact_native == ResolvedScale()

    def test_happy_path_crop_applied_before_scale(self) -> None:
        """The scale target sees the cropped dimensions, not the source's."""
        resolved = resolve_scale(
            Resolution.parse("3840x1600"), None, CropParams(left=140), _spec("1920"), 2,
        )
        assert (resolved.width, resolved.height) == (1918, 830)

    def test_properties_contain_stays_inside_and_on_grid(self) -> None:
        """For arbitrary sources/crops/granularities, a contained result's
        displayed rect stays inside the box and both dimensions stay on the
        granularity grid — quantization may only shrink toward the box."""
        dims = st.integers(min_value=16, max_value=4096)
        margin = st.integers(min_value=0, max_value=64)

        @given(
            width=dims, height=dims, sar_num=st.integers(min_value=1, max_value=3),
            sar_den=st.integers(min_value=1, max_value=3), left=margin, right=margin,
            top=margin, bottom=margin, granularity=st.sampled_from([2, 4, 8, 16]),
            box_w=st.integers(min_value=16, max_value=4096),
            box_h=st.none() | st.integers(min_value=16, max_value=4096),
        )
        def check(width, height, sar_num, sar_den, left, right, top, bottom, granularity, box_w, box_h) -> None:
            cropped_w, cropped_h = width - left - right, height - top - bottom
            if cropped_w <= 0 or cropped_h <= 0:
                return
            aspect = Fraction(cropped_w) * Fraction(sar_num, sar_den) / cropped_h
            spec = ScaleSpec(
                mode=ScaleMode.CONTAIN, width=box_w, height=box_h, algorithm=ALG,
            )
            try:
                resolved = resolve_scale(
                    Resolution.parse(f"{width}x{height}"), Fraction(sar_num, sar_den),
                    CropParams(top=top, bottom=bottom, left=left, right=right), spec, granularity,
                )
            except ValueError:
                # The loud infeasibility raise — coherent only when the box
                # admits no on-granularity height at all.
                assert box_w < granularity or (box_w // aspect) < granularity
                return
            if resolved.mode == ScaleMode.OFF:
                return
            assert resolved.width % granularity == 0 and resolved.height % granularity == 0
            assert aspect * resolved.height <= box_w
            if box_h is not None:
                assert resolved.height <= box_h

        check()

    def test_properties_cover_reaches_box_and_never_upscales(self) -> None:
        """A covered result reaches the box on both axes (ceil-quantized)
        and never upscales the displayed rect — the storage height IS the
        displayed height; the storage width may round up onto the grid
        while the displayed width stays put (SAR absorbs the rounding)."""
        dims = st.integers(min_value=16, max_value=4096)

        @given(
            width=dims, height=dims, granularity=st.sampled_from([2, 4, 8, 16]),
            box_w=st.integers(min_value=16, max_value=4096),
            box_h=st.integers(min_value=16, max_value=4096),
        )
        def check(width, height, granularity, box_w, box_h) -> None:
            try:
                resolved = resolve_scale(
                    Resolution.parse(f"{width}x{height}"), None, CropParams(),
                    ScaleSpec(mode=ScaleMode.COVER, width=box_w, height=box_h, algorithm=ALG),
                    granularity,
                )
            except ValueError:
                # The never-upscale raise: the covering height cannot land on
                # the grid without growing past the source's.
                return
            if resolved.mode == ScaleMode.OFF:
                return
            aspect = Fraction(width, height)
            assert resolved.width % granularity == 0 and resolved.height % granularity == 0
            assert aspect * resolved.height >= box_w
            assert resolved.height >= box_h
            assert resolved.height <= height

        check()

    def test_properties_contain_never_upscales_square(self) -> None:
        """On square sources a contained result never exceeds the cropped
        dimensions in either axis."""
        dims = st.integers(min_value=16, max_value=4096)

        @given(width=dims, height=dims, box_w=st.integers(min_value=16, max_value=8192))
        def check(width, height, box_w) -> None:
            try:
                resolved = resolve_scale(
                    Resolution.parse(f"{width}x{height}"), None, CropParams(),
                    ScaleSpec(mode=ScaleMode.CONTAIN, width=box_w, height=None, algorithm=ALG),
                    2,
                )
            except ValueError:
                # The loud infeasibility raise: extreme aspects at tiny boxes
                # admit no on-grid height.
                return
            if resolved.mode == ScaleMode.OFF:
                return
            assert resolved.width <= width and resolved.height <= height

        check()


class TestComparisonScale:
    """The measurement alignment policy."""

    def test_happy_path_reference_scaled_attempt_untouched(self) -> None:
        """The reference is scaled to the output dims; the distorted side
        needs no scaling (attempts are produced at the output dims)."""
        assert comparison_scale(ResolvedScale()) == (None, None)
        applied = ResolvedScale(mode=ScaleMode.CONTAIN, width=1920, height=800, algorithm=ALG)
        assert comparison_scale(applied) == ((1920, 800), None)
