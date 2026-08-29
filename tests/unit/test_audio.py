"""Unit tests for audio processing components.

Covers:
- Strategy check() methods (matching and non-matching filenames)
- AudioEngine.build_plan() graph shape
- ConversionStrategy plan() path construction
- _scale_bitrate() channel-count multiplier and string parsing
- _two_pass_loudnorm loudnorm JSON parsing (mocked run_ffmpeg_async)
- streams_filter_plain_regex() with include-only, exclude-only, and combined patterns
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pyqenc.constants import (
    AUDIO_CH_51,
    AUDIO_CH_71,
    AUDIO_STEM_SEPARATOR,
    NORMALISED_PREFIXES,
    TIME_SEPARATOR_MS,
)
from pyqenc.phases.audio import (
    AudioEngine,
    ConversionStrategy,
    DownmixStrategy51to20NBoost,
    DownmixStrategy51to20Night,
    DownmixStrategy51to20Std,
    DownmixStrategy71to51,
    DynaudnormStrategy,
    NormStrategy,
    _scale_bitrate,
    _two_pass_loudnorm,
)
from pyqenc.app_config import load_app_config
from pyqenc.phases.extraction import streams_filter_plain_regex
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _path(name: str) -> Path:
    """Return a fake Path with the given filename (no real file needed for check())."""
    return Path("/fake/dir") / name


def _stem(layout: str) -> str:
    """Return a realistic extraction-phase stem for the given channel layout."""
    return f"#02 ID=2 (audio-ac3) lang=rus ch={layout} start=0.028"


_AUDIO_CFG = load_app_config(default_only=True).audio


def _flat_conv_strategy() -> ConversionStrategy:
    """Return a ConversionStrategy using the current flat API."""
    return ConversionStrategy(codec="aac", bitrate_per_channel="96k", extension=".m4a")


# ---------------------------------------------------------------------------
# Strategy check() — matching filenames
# ---------------------------------------------------------------------------

class TestDownmixStrategy71to51Check:
    def setup_method(self) -> None:
        self.s = DownmixStrategy71to51()

    def test_matches_71_tag(self) -> None:
        assert self.s.check(_path(f"{_stem('7.1')}.mka")) is True

    def test_rejects_51_tag(self) -> None:
        assert self.s.check(_path(f"{_stem('5.1')}.mka")) is False

    def test_rejects_20_tag(self) -> None:
        assert self.s.check(_path(f"{_stem('2.0')}.mka")) is False

    def test_rejects_stereo_tag(self) -> None:
        assert self.s.check(_path(f"{_stem('stereo')}.mka")) is False

    def test_rejects_normalised_output(self) -> None:
        # A 5.1 downmix output is not a raw source — 71→51 must not re-fire on it
        name = f"5{TIME_SEPARATOR_MS}1 {AUDIO_STEM_SEPARATOR} {_stem('7.1')}.flac"
        assert self.s.check(_path(name)) is False


class TestDownmixStrategy51to20Check:
    """All three 5.1→2.0 strategies share the same check() logic."""

    def setup_method(self) -> None:
        self.strategies = [
            DownmixStrategy51to20Std(_AUDIO_CFG),
            DownmixStrategy51to20Night(_AUDIO_CFG),
            DownmixStrategy51to20NBoost(_AUDIO_CFG),
        ]

    def test_matches_51_tag(self) -> None:
        p = _path(f"{_stem('5.1')}.mka")
        for s in self.strategies:
            assert s.check(p) is True, f"{s.strategy_short} should match ch=5.1"

    def test_rejects_71_tag(self) -> None:
        p = _path(f"{_stem('7.1')}.mka")
        for s in self.strategies:
            assert s.check(p) is False, f"{s.strategy_short} should reject ch=7.1"

    def test_rejects_20_tag(self) -> None:
        p = _path(f"{_stem('2.0')}.mka")
        for s in self.strategies:
            assert s.check(p) is False

    def test_matches_51_downmix_output(self) -> None:
        # The 7.1→5.1 downmix output has prefix "5․1" (TIME_SEPARATOR_MS) —
        # the 5.1→2.0 strategies SHOULD match it so the graph continues correctly.
        name = f"5{TIME_SEPARATOR_MS}1 {AUDIO_STEM_SEPARATOR} {_stem('7.1')}.flac"
        p = _path(name)
        for s in self.strategies:
            assert s.check(p) is True, f"{s.strategy_short} should match 5.1 downmix output"

    def test_rejects_chained_20_output(self) -> None:
        # A 2.0 std output should not re-trigger any 5.1→2.0 strategy
        name = f"2{TIME_SEPARATOR_MS}0 std {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac"
        p = _path(name)
        for s in self.strategies:
            assert s.check(p) is False, f"{s.strategy_short} should reject 2.0 std output"


class TestNormStrategyCheck:
    def setup_method(self) -> None:
        self.s = NormStrategy(_AUDIO_CFG)

    def test_matches_plain_51_source(self) -> None:
        assert self.s.check(_path(f"{_stem('5.1')}.mka")) is True

    def test_matches_plain_71_source(self) -> None:
        assert self.s.check(_path(f"{_stem('7.1')}.mka")) is True

    def test_matches_plain_stereo_source(self) -> None:
        assert self.s.check(_path(f"{_stem('stereo')}.mka")) is True

    @pytest.mark.parametrize("prefix", list(NORMALISED_PREFIXES))
    def test_rejects_normalised_prefix(self, prefix: str) -> None:
        # A processed output starting with a normalised prefix is not a raw source
        name = f"{prefix} {_stem('5.1')}.flac"
        assert self.s.check(_path(name)) is False

    def test_rejects_dynaudnorm_output(self) -> None:
        # dynaudnorm output is not a raw source
        name = f"dynaudnorm {AUDIO_STEM_SEPARATOR} norm {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac"
        assert self.s.check(_path(name)) is False

    def test_rejects_any_processed_output(self) -> None:
        # Any file with ← in the name is not a raw source
        name = f"2{TIME_SEPARATOR_MS}0 std {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac"
        assert self.s.check(_path(name)) is False


class TestDynaudnormStrategyCheck:
    def setup_method(self) -> None:
        self.s = DynaudnormStrategy()

    @pytest.mark.parametrize("prefix", list(NORMALISED_PREFIXES))
    def test_matches_direct_normalised_output(self, prefix: str) -> None:
        # A direct normalised output (not raw, starts with normalised prefix)
        name = f"{prefix} {_stem('5.1')}.flac"
        assert self.s.check(_path(name)) is True

    def test_rejects_raw_source(self) -> None:
        # Raw extracted files are not normalised outputs
        assert self.s.check(_path(f"{_stem('5.1')}.mka")) is False

    def test_rejects_dynaudnorm_output(self) -> None:
        # dynaudnorm output must not trigger another dynaudnorm pass
        name = f"dynaudnorm {AUDIO_STEM_SEPARATOR} norm {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac"
        assert self.s.check(_path(name)) is False

    def test_rejects_plain_downmix_output(self) -> None:
        # 5.1 downmix output doesn't start with a normalised prefix
        name = f"5{TIME_SEPARATOR_MS}1 {AUDIO_STEM_SEPARATOR} {_stem('7.1')}.flac"
        assert self.s.check(_path(name)) is False


class TestConversionStrategyCheck:
    def test_always_false(self) -> None:
        s = _flat_conv_strategy()
        assert s.check(_path("anything.flac")) is False
        assert s.check(_path(f"norm {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac")) is False


# ---------------------------------------------------------------------------
# _scale_bitrate — channel count multiplier and string parsing
# ---------------------------------------------------------------------------

class TestScaleBitrateChannelCounts:
    """Bug condition: wrong channel count multiplier or incorrect string parsing in _scale_bitrate."""

    def test_20_layout_doubles_base(self) -> None:
        """2.0 layout has 2 channels → 96k × 2 = 192k."""
        assert _scale_bitrate("96k", _path(f"{_stem('2.0')}.flac")) == "192k"

    def test_stereo_layout_doubles_base(self) -> None:
        """stereo alias resolves to 2 channels → 96k × 2 = 192k."""
        assert _scale_bitrate("96k", _path(f"{_stem('stereo')}.flac")) == "192k"

    def test_51_layout_multiplies_by_six(self) -> None:
        """5.1 layout has 6 channels → 96k × 6 = 576k."""
        assert _scale_bitrate("96k", _path(f"{_stem('5.1')}.flac")) == "576k"

    def test_71_layout_multiplies_by_eight(self) -> None:
        """7.1 layout has 8 channels → 96k × 8 = 768k."""
        assert _scale_bitrate("96k", _path(f"{_stem('7.1')}.flac")) == "768k"

    def test_unknown_layout_defaults_to_stereo(self) -> None:
        """Unknown layout tag falls back to the stereo (2-channel) default."""
        assert _scale_bitrate("96k", _path("unknown_layout.flac")) == "192k"

    def test_uppercase_k_suffix_is_accepted(self) -> None:
        """Upper-case K suffix (e.g. "96K") is parsed the same as lower-case."""
        assert _scale_bitrate("96K", _path(f"{_stem('5.1')}.flac")) == "576k"


# ---------------------------------------------------------------------------
# Strategy short identifiers and output_path helper
# ---------------------------------------------------------------------------

class TestStrategyShortAndOutputPath:
    def test_strategy_shorts(self) -> None:
        _dot = TIME_SEPARATOR_MS   # ASCII dot is sanitized at construction time
        assert DownmixStrategy71to51().strategy_short == f"5{_dot}1"
        assert DownmixStrategy51to20Std(_AUDIO_CFG).strategy_short == f"2{_dot}0 std"
        assert DownmixStrategy51to20Night(_AUDIO_CFG).strategy_short == f"2{_dot}0 night"
        assert DownmixStrategy51to20NBoost(_AUDIO_CFG).strategy_short == f"2{_dot}0 nboost"
        assert NormStrategy(_AUDIO_CFG).strategy_short == "norm"
        assert DynaudnormStrategy().strategy_short == "dynaudnorm"
        assert _flat_conv_strategy().strategy_short == "aac"

    def test_output_path_naming(self) -> None:
        s = NormStrategy(_AUDIO_CFG)
        source = _path(f"{_stem('5.1')}.mka")
        out = s.output_path(source)
        assert out.name == f"norm {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac"

    def test_output_path_custom_extension(self) -> None:
        s = _flat_conv_strategy()
        source = _path(f"norm {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.flac")
        out = s.output_path(source, extension="m4a")
        assert out.name == f"aac {AUDIO_STEM_SEPARATOR} norm {AUDIO_STEM_SEPARATOR} {_stem('5.1')}.m4a"


# ---------------------------------------------------------------------------
# AudioEngine — duplicate strategy_short raises ValueError
# ---------------------------------------------------------------------------

class TestAudioEngineDuplicateShort:
    def test_raises_on_duplicate(self) -> None:
        s1 = NormStrategy(_AUDIO_CFG)
        s2 = NormStrategy(_AUDIO_CFG)  # same strategy_short = "norm"
        with pytest.raises(ValueError, match="Duplicate strategy_short"):
            AudioEngine(strategies=[s1, s2])

    def test_raises_when_finalizer_collides(self) -> None:
        # ConversionStrategy has short "aac"; create a fake one with same short
        class _FakeAac(NormStrategy):
            def __init__(self) -> None:
                super().__init__(_AUDIO_CFG)
                self.strategy_short = "aac"

        with pytest.raises(ValueError, match="Duplicate strategy_short"):
            AudioEngine(
                strategies=[_FakeAac()],
                finalizer=_flat_conv_strategy(),
            )


# ---------------------------------------------------------------------------
# AudioEngine.build_plan() — graph shape
# ---------------------------------------------------------------------------

class TestAudioEngineBuildPlan:
    """Verify the task graph shape against the target processing graph.

    Uses a temporary directory with fake (empty) source files so build_plan()
    can discover them without running any real ffmpeg commands.
    """

    @staticmethod
    def _convert_filter() -> str:
        """Build the correct convert_filter using the actual separator constants."""
        return f"^(norm|dynaudnorm|2{TIME_SEPARATOR_MS}0 (std|night|nboost)) {AUDIO_STEM_SEPARATOR}"

    def _make_engine(self) -> AudioEngine:
        return AudioEngine(
            strategies=[
                DownmixStrategy71to51(),
                DownmixStrategy51to20Std(_AUDIO_CFG),
                DownmixStrategy51to20Night(_AUDIO_CFG),
                DownmixStrategy51to20NBoost(_AUDIO_CFG),
                NormStrategy(_AUDIO_CFG),
                DynaudnormStrategy(),
            ],
            finalizer=_flat_conv_strategy(),
        )

    def test_51_source_graph(self, tmp_path: Path) -> None:
        """A 5.1 source produces: 3 downmix + 1 norm + 4 dynaudnorm + 8 aac = 16 tasks.

        DynaudnormStrategy fires on all 4 normalised outputs (norm←, 2.0std←,
        2.0night←, 2.0nboost←) because NORMALISED_PREFIXES uses TIME_SEPARATOR_MS.
        """
        stem = _stem("5.1")
        (tmp_path / f"{stem}.flac").touch()

        engine = self._make_engine()
        plan = engine.build_plan(tmp_path, self._convert_filter())

        strategy_shorts = [t.strategy.strategy_short for t in plan.tasks]
        _dot = TIME_SEPARATOR_MS

        assert strategy_shorts.count(f"2{_dot}0 std")    == 1
        assert strategy_shorts.count(f"2{_dot}0 night")  == 1
        assert strategy_shorts.count(f"2{_dot}0 nboost") == 1
        assert strategy_shorts.count("norm")       == 1
        assert strategy_shorts.count("dynaudnorm") == 4
        assert strategy_shorts.count("aac")        == 8
        assert strategy_shorts.count(f"5{_dot}1")  == 0

    def test_71_source_graph(self, tmp_path: Path) -> None:
        """A 7.1 source expands to 21 tasks:
        - 5.1 downmix (1)
        - norm on raw 7.1 (1) + norm on 5.1 downmix output (1) = 2 norm
        - 3 x 2.0 downmix from 5.1 output (3)
        - dynaudnorm on: norm←7.1, norm←5.1, 2.0std, 2.0night, 2.0nboost = 5
        - aac on: norm←7.1, norm←5.1, 2.0std, 2.0night, 2.0nboost,
                  dyn←norm←7.1, dyn←norm←5.1, dyn←std, dyn←night, dyn←nboost = 10
        """
        stem = _stem("7.1")
        (tmp_path / f"{stem}.flac").touch()

        engine = self._make_engine()
        plan = engine.build_plan(tmp_path, self._convert_filter())

        strategy_shorts = [t.strategy.strategy_short for t in plan.tasks]
        _dot = TIME_SEPARATOR_MS

        assert strategy_shorts.count(f"5{_dot}1")         == 1
        assert strategy_shorts.count("norm")               == 2   # once on raw 7.1, once on 5.1 output
        assert strategy_shorts.count(f"2{_dot}0 std")     == 1
        assert strategy_shorts.count(f"2{_dot}0 night")   == 1
        assert strategy_shorts.count(f"2{_dot}0 nboost")  == 1
        assert strategy_shorts.count("dynaudnorm")         == 5   # norm←7.1, norm←5.1, std, night, nboost
        assert strategy_shorts.count("aac")                == 10

    def test_stereo_source_only_norm(self, tmp_path: Path) -> None:
        """A stereo source should only get norm + dynaudnorm + aac (no downmix)."""
        stem = _stem("stereo")
        (tmp_path / f"{stem}.flac").touch()

        engine = self._make_engine()
        plan = engine.build_plan(tmp_path, self._convert_filter())

        strategy_shorts = [t.strategy.strategy_short for t in plan.tasks]

        assert strategy_shorts.count("norm")       == 1
        assert strategy_shorts.count("dynaudnorm") == 1
        assert strategy_shorts.count("aac")        == 2
        assert strategy_shorts.count("5.1")        == 0
        assert strategy_shorts.count("2.0 std")    == 0

    def test_no_duplicate_outputs(self, tmp_path: Path) -> None:
        """build_plan() must not produce duplicate output paths."""
        stem = _stem("5.1")
        (tmp_path / f"{stem}.flac").touch()

        engine = self._make_engine()
        plan = engine.build_plan(tmp_path, self._convert_filter())

        outputs = [t.output for t in plan.tasks]
        assert len(outputs) == len(set(outputs)), "Duplicate output paths in plan"

    def test_empty_directory(self, tmp_path: Path) -> None:
        engine = self._make_engine()
        plan = engine.build_plan(tmp_path, self._convert_filter())
        assert plan.tasks == []
        assert plan.found_files == 0

    def test_graph_terminates_naturally(self, tmp_path: Path) -> None:
        """dynaudnorm outputs must not trigger any further strategy."""
        stem = _stem("5.1")
        (tmp_path / f"{stem}.flac").touch()

        engine = self._make_engine()
        plan = engine.build_plan(tmp_path, self._convert_filter())

        dynaudnorm_outputs = {
            t.output for t in plan.tasks if t.strategy.strategy_short == "dynaudnorm"
        }
        # None of the dynaudnorm outputs should appear as a source for another
        # non-aac task (aac is fine — it's the finalizer)
        non_aac_sources = {
            t.source for t in plan.tasks if t.strategy.strategy_short != "aac"
        }
        assert dynaudnorm_outputs.isdisjoint(non_aac_sources), (
            "dynaudnorm outputs are being fed into further non-aac strategies"
        )


# ---------------------------------------------------------------------------
# _two_pass_loudnorm — loudnorm JSON parsing (mocked run_ffmpeg_async)
# ---------------------------------------------------------------------------

_SAMPLE_LOUDNORM_JSON = {
    "input_i":      "-23.5",
    "input_tp":     "-1.2",
    "input_lra":    "6.8",
    "input_thresh": "-33.5",
    "target_offset": "0.5",
}

# Flatten the JSON into a single line as ffmpeg actually outputs it
_SAMPLE_STDERR_FLAT = [
    "ffmpeg version 6.0",
    f"[Parsed_loudnorm_0 @ 0x...] {json.dumps(_SAMPLE_LOUDNORM_JSON)}",
    "size=N/A time=00:01:00.00 bitrate=N/A",
]


def _make_ffmpeg_result(
    stderr_lines: list[str],
    returncode: int = 0,
) -> FFmpegRunResult:
    return FFmpegRunResult(
        returncode   = returncode,
        success      = returncode == 0,
        stderr_lines = stderr_lines,
    )


class TestTwoPassLoudnorm:
    async def test_successful_two_pass(self, tmp_path: Path) -> None:
        source = tmp_path / "source.flac"
        source.touch()
        output = tmp_path / "output.flac"

        pass1_result = _make_ffmpeg_result(_SAMPLE_STDERR_FLAT)
        pass2_result = _make_ffmpeg_result([])

        with patch(
            "pyqenc.phases.audio.run_ffmpeg_async",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.side_effect = [pass1_result, pass2_result]
            await _two_pass_loudnorm(source, output)

        assert mock_run.call_count == 2

        # Pass 1 should use output_file=None (analysis only)
        pass1_call = mock_run.call_args_list[0]
        assert pass1_call.kwargs.get("output_file") is None or pass1_call.args[1] is None

        # Pass 2 should use output_file=output
        pass2_call = mock_run.call_args_list[1]
        output_arg = pass2_call.kwargs.get("output_file") or pass2_call.args[1]
        assert output_arg == output

    async def test_pass2_cmd_contains_measured_values(self, tmp_path: Path) -> None:
        source = tmp_path / "source.flac"
        source.touch()
        output = tmp_path / "output.flac"

        pass1_result = _make_ffmpeg_result(_SAMPLE_STDERR_FLAT)
        pass2_result = _make_ffmpeg_result([])

        with patch(
            "pyqenc.phases.audio.run_ffmpeg_async",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.side_effect = [pass1_result, pass2_result]
            await _two_pass_loudnorm(source, output)

        pass2_cmd = mock_run.call_args_list[1].args[0]
        pass2_cmd_str = " ".join(str(a) for a in pass2_cmd)

        assert "linear=true" in pass2_cmd_str
        assert _SAMPLE_LOUDNORM_JSON["input_i"]  in pass2_cmd_str
        assert _SAMPLE_LOUDNORM_JSON["input_tp"] in pass2_cmd_str

    async def test_extra_filters_prepended_in_both_passes(self, tmp_path: Path) -> None:
        source = tmp_path / "source.flac"
        source.touch()
        output = tmp_path / "output.flac"

        extra = ["pan=stereo|FL=FL+0.707*FC|FR=FR+0.707*FC"]

        pass1_result = _make_ffmpeg_result(_SAMPLE_STDERR_FLAT)
        pass2_result = _make_ffmpeg_result([])

        with patch(
            "pyqenc.phases.audio.run_ffmpeg_async",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.side_effect = [pass1_result, pass2_result]
            await _two_pass_loudnorm(source, output, extra_filters=extra)

        for call in mock_run.call_args_list:
            cmd_str = " ".join(str(a) for a in call.args[0])
            assert extra[0] in cmd_str, "extra filter must appear in both passes"

    async def test_raises_when_json_not_found(self, tmp_path: Path) -> None:
        source = tmp_path / "source.flac"
        source.touch()
        output = tmp_path / "output.flac"

        pass1_result = _make_ffmpeg_result(["no json here at all"])

        with patch(
            "pyqenc.phases.audio.run_ffmpeg_async",
            new_callable=AsyncMock,
            return_value=pass1_result,
        ):
            with pytest.raises(RuntimeError, match="parseable JSON"):
                await _two_pass_loudnorm(source, output)

    async def test_raises_when_pass2_fails(self, tmp_path: Path) -> None:
        source = tmp_path / "source.flac"
        source.touch()
        output = tmp_path / "output.flac"

        pass1_result = _make_ffmpeg_result(_SAMPLE_STDERR_FLAT)
        pass2_result = _make_ffmpeg_result([], returncode=1)

        with patch(
            "pyqenc.phases.audio.run_ffmpeg_async",
            new_callable=AsyncMock,
        ) as mock_run:
            mock_run.side_effect = [pass1_result, pass2_result]
            with pytest.raises(RuntimeError, match="pass 2 failed"):
                await _two_pass_loudnorm(source, output)


# ---------------------------------------------------------------------------
# streams_filter_plain_regex()
# ---------------------------------------------------------------------------

class _FakeStream:
    """Minimal stand-in for StreamBase — only display_name() is needed."""

    def __init__(self, name: str) -> None:
        self._name = name

    def display_name(self, *_: int) -> str:
        return self._name


def _fake_tracks(names: list[str]) -> list[_FakeStream]:  # type: ignore[return]
    return [_FakeStream(n) for n in names]


class TestStreamsFilterPlainRegex:
    _TRACKS = [
        "#01 ID=1 (video-h264) lang=eng res=1920x1080 start=0.0.mkv",
        "#02 ID=2 (audio-ac3) lang=eng ch=5.1(side) start=0.028.mka",
        "#03 ID=3 (audio-ac3) lang=rus ch=5.1(side) start=0.028.mka",
        "#04 ID=4 (subtitle-subrip) lang=eng start=0.0.srt",
        "#05 ID=5 (subtitle-subrip) lang=rus start=0.0.srt",
    ]

    def _filter(
        self,
        include: str | None = None,
        exclude: str | None = None,
    ) -> list[str]:
        from pyqenc.phases.extraction import streams_filter_plain_regex as _f
        tracks = _fake_tracks(self._TRACKS)
        result = _f(tracks, include_pattern=include, exclude_pattern=exclude)  # type: ignore[arg-type]
        return [t.display_name() for t in result]

    def test_no_filters_returns_all(self) -> None:
        assert self._filter() == self._TRACKS

    def test_include_only_audio(self) -> None:
        result = self._filter(include=r"audio")
        assert all("audio" in r for r in result)
        assert len(result) == 2

    def test_exclude_rus(self) -> None:
        result = self._filter(exclude=r"lang=rus")
        assert all("lang=rus" not in r for r in result)
        assert len(result) == 3

    def test_include_audio_exclude_rus(self) -> None:
        result = self._filter(include=r"audio", exclude=r"lang=rus")
        assert result == [self._TRACKS[1]]  # only eng audio

    def test_exclude_takes_precedence_over_include(self) -> None:
        # include everything, but exclude video
        result = self._filter(include=r".*", exclude=r"video")
        assert all("video" not in r for r in result)

    def test_include_pattern_case_insensitive(self) -> None:
        result = self._filter(include=r"AUDIO")
        assert len(result) == 2

    def test_empty_include_pattern_matches_all(self) -> None:
        # An empty string pattern matches everything
        result = self._filter(include=r"")
        assert result == self._TRACKS

    def test_include_video_only(self) -> None:
        result = self._filter(include=r"video")
        assert len(result) == 1
        assert "video" in result[0]

    def test_exclude_all_subtitles(self) -> None:
        result = self._filter(exclude=r"subtitle")
        assert all("subtitle" not in r for r in result)
        assert len(result) == 3

    def test_no_match_include_returns_empty(self) -> None:
        result = self._filter(include=r"nonexistent_codec")
        assert result == []

