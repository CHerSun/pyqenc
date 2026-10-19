"""Unit tests for the audio filter classes: validation + resolve behaviour.

Each test names the concrete bug it guards against, per project standards, and
checks observable behaviour (validation errors, the returned :class:`FilterStep`),
never internal representation.
"""
# CHerSun 2026

from typing import get_args

import pytest
from pydantic import ValidationError

from pyqenc.audio.filters import (
    DownmixInstance,
    DynAudNormInstance,
    EncodeInstance,
    EncodeParams,
    FilterInstance,
    LoudNormInstance,
    PassthroughInstance,
    PeakNormInstance,
)
from pyqenc.audio.layout import ChannelLayout
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult

_STEREO = ChannelLayout.parse("2.0")
_51     = ChannelLayout.parse("5.1")


def _measurement(stderr_lines: list[str], returncode: int = 0) -> FFmpegRunResult:
    """Build a fake measurement-pass result carrying scraped stderr."""
    return FFmpegRunResult(
        returncode   = returncode,
        success      = returncode == 0,
        stderr_lines = stderr_lines,
    )


class TestShippedFilters:
    """The shipped filter set is exactly the six union members."""

    def test_union_tags_are_the_shipped_six(self) -> None:
        # Bug: the closed set drifting — a filter class forgotten in the union
        # (config then rejects a type the class provides) or a stray member
        # (config accepts a type nothing implements).
        union = get_args(FilterInstance)[0]
        tags = {member.model_fields["type"].default for member in get_args(union)}
        assert tags == {"peaknorm", "loudnorm", "dynaudnorm", "downmix", "encode", "passthrough"}


class TestParamValidation:
    """Each filter forbids unknown params; pyqenc-owned params stay required."""

    def test_peaknorm_rejects_unknown_param(self) -> None:
        # Bug: silently accepting a typo'd param (e.g. `target_db`) would apply
        # default behaviour instead of the user's intent.
        with pytest.raises(ValidationError):
            PeakNormInstance(target_dbfs=-1.0, bogus=1)  # ty: ignore[unknown-argument] — rejection is the test's subject

    def test_passthrough_rejects_any_param(self) -> None:
        # Bug: passthrough accepting transformation params would imply behaviour
        # it does not have.
        with pytest.raises(ValidationError):
            PassthroughInstance(target_dbfs=-1.0)  # ty: ignore[unknown-argument] — rejection is the test's subject


class TestUnsetParamsDelegateToffmpeg:
    """Unset ffmpeg-passthrough params are omitted; ffmpeg owns their defaults."""

    def test_bare_dynaudnorm_emits_filter_name_only(self) -> None:
        # Bug: emitting `dynaudnorm=...None...` (or a dangling `=`) breaks the
        # ffmpeg invocation; a fully-unset filter is just its name.
        assert DynAudNormInstance().resolve(_STEREO, None).af == "dynaudnorm"

    def test_partial_dynaudnorm_emits_only_set_options(self) -> None:
        # Bug: restating ffmpeg's defaults ourselves would freeze them in
        # pyqenc — only the stated param may appear.
        step = DynAudNormInstance(maxgain=3.0).resolve(_STEREO, None)
        assert step.af == "dynaudnorm=maxgain=3.0"

    def test_bare_loudnorm_pass1_keeps_mechanics(self) -> None:
        # print_format=json is pass mechanics, not a user param — always emitted.
        assert LoudNormInstance().resolve(_STEREO, None).af == "loudnorm=print_format=json"

    def test_partial_loudnorm_pass2_keeps_mechanics(self) -> None:
        # Unset tp/lra vanish; linear/measured_*/offset stay (pass mechanics,
        # never user params) — the fragment shape is uniform in both passes.
        f = LoudNormInstance(i=-23.0)
        json_block = (
            '[Parsed_loudnorm_0 @ 0x0] '
            '{"input_i":"-27.5","input_tp":"-5.2","input_lra":"9.1",'
            '"input_thresh":"-38.1","target_offset":"0.3"}'
        )
        step = f.resolve(_STEREO, _measurement(["ffmpeg version 6.0", json_block]))
        assert step.af == (
            "loudnorm=I=-23.0:linear=true"
            ":measured_I=-27.5:measured_TP=-5.2:measured_LRA=9.1"
            ":measured_thresh=-38.1:offset=0.3:print_format=none"
        )


class TestDownmixNoOp:
    """Downmix is reduction-only: no fragment when source <= target."""

    def test_stereo_to_stereo_is_noop(self) -> None:
        # Bug: re-folding an already-stereo source needlessly re-encodes and can
        # alter the signal; a no-op must emit an empty -af contribution.
        step = DownmixInstance(to="2.0").resolve(_STEREO, None)
        assert step.af == ""
        assert step.out_layout.normalized == "2.0"

    def test_51_to_20_emits_pan_fragment(self) -> None:
        # Bug: failing to emit the fold on a 5.1 source leaves a 6-channel output
        # where stereo was requested.
        step = DownmixInstance(to="2.0", matrix="std").resolve(_51, None)
        assert step.af == "aformat=sample_fmts=flt,pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5"
        assert step.out_layout.normalized == "2.0"


class TestEncodeFilter:
    """Encode contributes no -af and sets the terminal output format."""

    def test_encode_sets_output_format_no_af(self) -> None:
        # Bug: an encode filter injecting an -af fragment would corrupt the
        # filter chain; it only sets the output target.
        step = EncodeInstance(codec="aac", bitrate_per_channel="64k", extension="m4a").resolve(_STEREO, None)
        assert step.af == ""
        assert step.output_format == EncodeParams(codec="aac", bitrate_per_channel="64k", extension="m4a")


class TestDynAudNorm:
    """Dynaudnorm is single-pass and ignores last_output."""

    def test_single_pass_fragment(self) -> None:
        # Bug: requesting a measurement pass for a single-pass filter would waste
        # an ffmpeg invocation and could measure the wrong signal.
        f = DynAudNormInstance(framelen=500, gausssize=31, peak=0.95, maxgain=10.0, targetrms=0.0)
        step = f.resolve(_STEREO, None)
        assert step.needs_pass is False
        assert step.af == "dynaudnorm=framelen=500:gausssize=31:peak=0.95:maxgain=10.0:targetrms=0.0"


class TestTwoPassPeakNorm:
    """Peaknorm measures on the first call, applies the gain on the second."""

    def test_first_call_requests_measurement(self) -> None:
        # Bug: applying a gain before measuring would use an undefined value.
        step = PeakNormInstance(target_dbfs=-1.0).resolve(_STEREO, None)
        assert step.needs_pass is True
        assert step.af == "astats"

    def test_second_call_applies_measured_gain(self) -> None:
        # Bug: mis-scraping max_volume (or ignoring it) would apply the wrong
        # gain, over/under-shooting the target peak.
        f = PeakNormInstance(target_dbfs=-1.0)
        result = _measurement(["[Parsed_astats_1 @ 0x0] Peak level dB: -6.5"])
        step = f.resolve(_STEREO, result)
        assert step.needs_pass is False
        # gain = target(-1.0) - measured(-6.5) = 5.5 dB
        assert step.af == "volume=5.5000dB"

    def test_missing_measurement_raises(self) -> None:
        # Bug: proceeding without a parseable measurement would silently produce
        # an unnormalised file.
        f = PeakNormInstance(target_dbfs=-1.0)
        with pytest.raises(RuntimeError, match="parseable peak volume"):
            f.resolve(_STEREO, _measurement(["no measurement here"], returncode=1))


class TestTwoPassLoudNorm:
    """Loudnorm analyses on the first call, linear-normalises on the second."""

    def test_first_call_requests_json_analysis(self) -> None:
        # Bug: skipping the analysis pass forces a non-linear single-pass norm
        # that does not hit the configured integrated-loudness target.
        step = LoudNormInstance(i=-23.0, tp=-1.0, lra=7.0).resolve(_STEREO, None)
        assert step.needs_pass is True
        assert step.af == "loudnorm=I=-23.0:TP=-1.0:LRA=7.0:print_format=json"

    def test_second_call_folds_measured_values(self) -> None:
        # Bug: not feeding measured_* values back means the pass-2 normalisation
        # is not linear and misses the target loudness.
        f = LoudNormInstance(i=-23.0, tp=-1.0, lra=7.0)
        json_block = (
            '[Parsed_loudnorm_0 @ 0x0] '
            '{"input_i":"-27.5","input_tp":"-5.2","input_lra":"9.1",'
            '"input_thresh":"-38.1","target_offset":"0.3"}'
        )
        step = f.resolve(_STEREO, _measurement(["ffmpeg version 6.0", json_block]))
        assert step.needs_pass is False
        assert step.af == (
            "loudnorm=I=-23.0:TP=-1.0:LRA=7.0:linear=true"
            ":measured_I=-27.5:measured_TP=-5.2:measured_LRA=9.1"
            ":measured_thresh=-38.1:offset=0.3:print_format=none"
        )

    def test_unparseable_json_raises(self) -> None:
        # Bug: proceeding past a missing/garbled JSON block would silently skip
        # normalisation.
        f = LoudNormInstance(i=-23.0, tp=-1.0, lra=7.0)
        with pytest.raises(RuntimeError, match="JSON"):
            f.resolve(_STEREO, _measurement(["no json here"], returncode=1))


class TestPassthroughStub:
    """Passthrough is a valid config filter but fails loud on execution."""

    def test_resolve_raises_not_implemented(self) -> None:
        # Bug: a passthrough chain silently producing an incorrect/incomplete
        # file. Until implemented it must fail loudly.
        with pytest.raises(NotImplementedError, match="not implemented yet"):
            PassthroughInstance().resolve(_STEREO, None)
