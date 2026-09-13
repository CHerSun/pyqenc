"""Unit tests for the filter-type registry and the initial filter classes.

Each test names the concrete bug it guards against, per project standards, and
checks observable behaviour (registry membership, the returned :class:`FilterStep`,
validation errors), never internal representation.
"""
# CHerSun 2026

from collections.abc import Iterator

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from pyqenc.audio.filters import (
    _FILTER_REGISTRY,
    DownmixFilter,
    DownmixParams,
    DynAudNormFilter,
    DynAudNormParams,
    EncodeFilter,
    EncodeFilterParams,
    EncodeParams,
    FilterStep,
    FilterType,
    LoudNormFilter,
    LoudNormParams,
    PassthroughFilter,
    PassthroughParams,
    PeakNormFilter,
    PeakNormParams,
    get_filter_class,
    register_filter,
    registered_type_ids,
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


class TestRegistryOpenness:
    """A newly registered type is usable with no edits to config/executor."""

    @pytest.fixture
    def temp_type(self) -> Iterator[type[FilterType]]:
        """Register a throwaway filter type and remove it after the test."""
        test_id = "test_only_type"

        class _TestParams(BaseModel):
            model_config = ConfigDict(extra="forbid")
            gain: float

        @register_filter
        class _TestFilter(FilterType):
            type_id      = test_id
            params_model = _TestParams

            def resolve(
                self,
                layout: ChannelLayout,
                last_output: FFmpegRunResult | None,
            ) -> FilterStep:
                return FilterStep(af="anull", needs_pass=False, out_layout=layout)

        try:
            yield _TestFilter
        finally:
            _FILTER_REGISTRY.pop(test_id, None)

    def test_registered_type_is_resolvable_by_id(self, temp_type: type[FilterType]) -> None:
        # Bug: a hidden hardcoded enumeration would leave a newly registered type
        # invisible to the registry, making the type set effectively closed.
        assert get_filter_class("test_only_type") is temp_type
        assert "test_only_type" in registered_type_ids()

    def test_registered_type_resolves_without_touching_executor(
        self, temp_type: type[FilterType],
    ) -> None:
        # Bug: extension requiring executor edits. A registered type must produce
        # a usable FilterStep purely through the resolve contract.
        instance = temp_type(temp_type.params_model(gain=1.0))
        step = instance.resolve(_STEREO, None)
        assert step.af == "anull"
        assert step.needs_pass is False


class TestDuplicateRegistration:
    """Two classes cannot claim the same type id."""

    def test_duplicate_id_raises_at_registration(self) -> None:
        # Bug: a silently overwritten registry entry would let one type shadow
        # another, changing behaviour of every chain that referenced the id.
        with pytest.raises(ValueError, match="Duplicate filter type id"):
            @register_filter
            class _Dup(FilterType):
                type_id      = "peaknorm"  # already registered
                params_model = PeakNormParams

                def resolve(
                    self,
                    layout: ChannelLayout,
                    last_output: FFmpegRunResult | None,
                ) -> FilterStep:
                    return FilterStep(af="", needs_pass=False, out_layout=layout)


class TestInitialRegistrations:
    """The six default types are registered under their expected ids."""

    def test_default_six_present(self) -> None:
        # Bug: a missing default registration would make a documented filter type
        # fail config validation as "unknown type".
        for type_id in ("peaknorm", "loudnorm", "dynaudnorm", "downmix", "encode", "passthrough"):
            assert type_id in _FILTER_REGISTRY


class TestParamValidation:
    """Each param model forbids unknown params and enforces required fields."""

    def test_peaknorm_rejects_unknown_param(self) -> None:
        # Bug: silently accepting a typo'd param (e.g. `target_db`) would apply
        # default behaviour instead of the user's intent.
        with pytest.raises(ValidationError):
            PeakNormParams(target_dbfs=-1.0, bogus=1)  # type: ignore[call-arg]

    def test_loudnorm_requires_all_targets(self) -> None:
        # Bug: a missing target silently applying a default deviates from the
        # user's configured loudness target.
        with pytest.raises(ValidationError):
            LoudNormParams(i=-23.0, tp=-1.0)  # type: ignore[call-arg]

    def test_passthrough_rejects_any_param(self) -> None:
        # Bug: passthrough accepting transformation params would imply behaviour
        # it does not have.
        with pytest.raises(ValidationError):
            PassthroughParams(target_dbfs=-1.0)  # type: ignore[call-arg]


class TestDownmixNoOp:
    """Downmix is reduction-only: no fragment when source <= target."""

    def test_stereo_to_stereo_is_noop(self) -> None:
        # Bug: re-folding an already-stereo source needlessly re-encodes and can
        # alter the signal; a no-op must emit an empty -af contribution.
        f = DownmixFilter(DownmixParams(to="2.0"))
        step = f.resolve(_STEREO, None)
        assert step.af == ""
        assert step.out_layout.normalized == "2.0"

    def test_51_to_20_emits_pan_fragment(self) -> None:
        # Bug: failing to emit the fold on a 5.1 source leaves a 6-channel output
        # where stereo was requested.
        f = DownmixFilter(DownmixParams(to="2.0", matrix="std"))
        step = f.resolve(_51, None)
        assert step.af == "pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5"
        assert step.out_layout.normalized == "2.0"


class TestEncodeFilter:
    """Encode contributes no -af and sets the terminal output format."""

    def test_encode_sets_output_format_no_af(self) -> None:
        # Bug: an encode filter injecting an -af fragment would corrupt the
        # filter chain; it only sets the output target.
        f = EncodeFilter(EncodeFilterParams(codec="aac", bitrate_per_channel="64k", extension="m4a"))
        step = f.resolve(_STEREO, None)
        assert step.af == ""
        assert step.output_format == EncodeParams(codec="aac", bitrate_per_channel="64k", extension="m4a")


class TestDynAudNorm:
    """Dynaudnorm is single-pass and ignores last_output."""

    def test_single_pass_fragment(self) -> None:
        # Bug: requesting a measurement pass for a single-pass filter would waste
        # an ffmpeg invocation and could measure the wrong signal.
        f = DynAudNormFilter(DynAudNormParams(framelen=500, gausssize=31, peak=0.95, maxgain=10.0, targetrms=0.0))
        step = f.resolve(_STEREO, None)
        assert step.needs_pass is False
        assert step.af == "dynaudnorm=framelen=500:gausssize=31:peak=0.95:maxgain=10.0:targetrms=0.0"


class TestTwoPassPeakNorm:
    """Peaknorm measures on the first call, applies the gain on the second."""

    def test_first_call_requests_measurement(self) -> None:
        # Bug: applying a gain before measuring would use an undefined value.
        f = PeakNormFilter(PeakNormParams(target_dbfs=-1.0))
        step = f.resolve(_STEREO, None)
        assert step.needs_pass is True
        assert step.af == "volumedetect"

    def test_second_call_applies_measured_gain(self) -> None:
        # Bug: mis-scraping max_volume (or ignoring it) would apply the wrong
        # gain, over/under-shooting the target peak.
        f = PeakNormFilter(PeakNormParams(target_dbfs=-1.0))
        result = _measurement(["[Parsed_volumedetect_0 @ 0x0] max_volume: -6.5 dB"])
        step = f.resolve(_STEREO, result)
        assert step.needs_pass is False
        # gain = target(-1.0) - measured(-6.5) = 5.5 dB
        assert step.af == "volume=5.5000dB"

    def test_missing_measurement_raises(self) -> None:
        # Bug: proceeding without a parseable measurement would silently produce
        # an unnormalised file.
        f = PeakNormFilter(PeakNormParams(target_dbfs=-1.0))
        with pytest.raises(RuntimeError, match="max_volume"):
            f.resolve(_STEREO, _measurement(["no measurement here"], returncode=1))


class TestTwoPassLoudNorm:
    """Loudnorm analyses on the first call, linear-normalises on the second."""

    def test_first_call_requests_json_analysis(self) -> None:
        # Bug: skipping the analysis pass forces a non-linear single-pass norm
        # that does not hit the configured integrated-loudness target.
        f = LoudNormFilter(LoudNormParams(i=-23.0, tp=-1.0, lra=7.0))
        step = f.resolve(_STEREO, None)
        assert step.needs_pass is True
        assert step.af == "loudnorm=I=-23.0:TP=-1.0:LRA=7.0:print_format=json"

    def test_second_call_folds_measured_values(self) -> None:
        # Bug: not feeding measured_* values back means the pass-2 normalisation
        # is not linear and misses the target loudness.
        f = LoudNormFilter(LoudNormParams(i=-23.0, tp=-1.0, lra=7.0))
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
        f = LoudNormFilter(LoudNormParams(i=-23.0, tp=-1.0, lra=7.0))
        with pytest.raises(RuntimeError, match="JSON"):
            f.resolve(_STEREO, _measurement(["no json here"], returncode=1))


class TestPassthroughStub:
    """Passthrough is a valid config filter but fails loud on execution."""

    def test_resolve_raises_not_implemented(self) -> None:
        # Bug: a passthrough chain silently producing an incorrect/incomplete
        # file. Until implemented it must fail loudly.
        f = PassthroughFilter(PassthroughParams())
        with pytest.raises(NotImplementedError, match="in-memory-stream"):
            f.resolve(_STEREO, None)
