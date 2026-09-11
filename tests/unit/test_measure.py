"""Unit tests for pyqenc/phases/measure.py helper functions.

Covers: _parse_duration, _screenshot_timestamps_count,
        _screenshot_timestamps_interval, _screenshot_filename,
        _resolve_crop, make_screenshots.

Run with: uv run python -m pytest tests/unit/test_measure.py
"""

# Feature: standalone-measure

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.constants import TIME_SEPARATOR_MS, TIME_SEPARATOR_SAFE
from pyqenc.phases.measure import (
    _parse_duration,
    _screenshot_filename,
    _screenshot_timestamps_count,
    _screenshot_timestamps_interval,
)

SEP  = TIME_SEPARATOR_SAFE  # ꞉
MSEP = TIME_SEPARATOR_MS    # ․


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    """Tests for _parse_duration."""

    # --- plain numeric ---

    def test_plain_integer(self) -> None:
        assert _parse_duration("30") == pytest.approx(30.0)

    def test_plain_float(self) -> None:
        assert _parse_duration("90.5") == pytest.approx(90.5)

    def test_plain_zero(self) -> None:
        assert _parse_duration("0") == pytest.approx(0.0)

    def test_plain_float_zero(self) -> None:
        assert _parse_duration("0.0") == pytest.approx(0.0)

    # --- human-friendly: seconds only ---

    def test_seconds_suffix(self) -> None:
        assert _parse_duration("30s") == pytest.approx(30.0)

    def test_seconds_suffix_float(self) -> None:
        assert _parse_duration("90.5s") == pytest.approx(90.5)

    # --- human-friendly: minutes ---

    def test_minutes_only(self) -> None:
        assert _parse_duration("5m") == pytest.approx(300.0)

    def test_minutes_and_seconds(self) -> None:
        assert _parse_duration("1m30s") == pytest.approx(90.0)

    # --- human-friendly: hours ---

    def test_hours_only(self) -> None:
        assert _parse_duration("1h") == pytest.approx(3600.0)

    def test_hours_and_minutes(self) -> None:
        assert _parse_duration("1h30m") == pytest.approx(5400.0)

    def test_hours_minutes_seconds(self) -> None:
        assert _parse_duration("1h30m45s") == pytest.approx(5445.0)

    def test_hours_and_seconds_no_minutes(self) -> None:
        assert _parse_duration("2h45s") == pytest.approx(7245.0)

    # --- whitespace tolerance ---

    def test_leading_trailing_whitespace(self) -> None:
        assert _parse_duration("  30s  ") == pytest.approx(30.0)

    # --- invalid input ---

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("   ")

    def test_letters_only_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("abc")

    def test_negative_plain_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("-5")

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("1x30y")

    def test_bare_unit_no_value_raises(self) -> None:
        # "m" alone has no numeric component — should raise
        with pytest.raises(ValueError):
            _parse_duration("m")


# ---------------------------------------------------------------------------
# _screenshot_timestamps_count
# ---------------------------------------------------------------------------


class TestScreenshotTimestampsCount:
    """Tests for _screenshot_timestamps_count."""

    def test_single_screenshot_midpoint(self) -> None:
        """One screenshot lands at the midpoint."""
        result = _screenshot_timestamps_count(10.0, 1)
        assert result == pytest.approx([5.0])

    def test_two_screenshots_thirds(self) -> None:
        """Two screenshots land at 1/3 and 2/3."""
        result = _screenshot_timestamps_count(9.0, 2)
        assert result == pytest.approx([3.0, 6.0])

    def test_three_screenshots_quarters(self) -> None:
        """Three screenshots land at 1/4, 2/4, 3/4."""
        result = _screenshot_timestamps_count(8.0, 3)
        assert result == pytest.approx([2.0, 4.0, 6.0])

    def test_all_timestamps_strictly_interior(self) -> None:
        """All timestamps must be strictly between 0 and duration."""
        result = _screenshot_timestamps_count(100.0, 10)
        assert all(0.0 < t < 100.0 for t in result)

    def test_count_matches_requested(self) -> None:
        """Exactly count timestamps returned for normal inputs."""
        result = _screenshot_timestamps_count(60.0, 20)
        assert len(result) == 20

    def test_evenly_spaced(self) -> None:
        """Consecutive timestamps differ by the same step."""
        result = _screenshot_timestamps_count(100.0, 4)
        step = 100.0 / 5
        for i, t in enumerate(result, start=1):
            assert t == pytest.approx(i * step)


# ---------------------------------------------------------------------------
# _screenshot_timestamps_interval
# ---------------------------------------------------------------------------


class TestScreenshotTimestampsInterval:
    """Tests for _screenshot_timestamps_interval."""

    def test_basic_interval(self) -> None:
        """Timestamps at multiples of interval up to duration."""
        result = _screenshot_timestamps_interval(10.0, 3.0)
        assert result == pytest.approx([3.0, 6.0, 9.0])

    def test_interval_equals_duration_returns_empty(self) -> None:
        """Interval >= duration → empty list."""
        assert _screenshot_timestamps_interval(5.0, 5.0) == []

    def test_interval_exceeds_duration_returns_empty(self) -> None:
        """Interval > duration → empty list."""
        assert _screenshot_timestamps_interval(5.0, 10.0) == []

    def test_first_timestamp_is_one_interval(self) -> None:
        """First timestamp is 1×interval, not 0."""
        result = _screenshot_timestamps_interval(60.0, 15.0)
        assert result[0] == pytest.approx(15.0)

    def test_all_timestamps_strictly_less_than_duration(self) -> None:
        """No timestamp reaches or exceeds duration."""
        result = _screenshot_timestamps_interval(10.0, 3.0)
        assert all(t < 10.0 for t in result)

    def test_exact_multiple_excluded(self) -> None:
        """When duration is an exact multiple of interval, last point is excluded."""
        # 3 intervals of 3.0 fit in 9.0 exactly; 4th would be 12.0 > 9.0
        result = _screenshot_timestamps_interval(9.0, 3.0)
        assert result == pytest.approx([3.0, 6.0])
        assert 9.0 not in result

    def test_small_interval_many_timestamps(self) -> None:
        """Many timestamps generated for small interval."""
        result = _screenshot_timestamps_interval(10.0, 1.0)
        assert len(result) == 9
        assert result[0] == pytest.approx(1.0)
        assert result[-1] == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# _screenshot_filename
# ---------------------------------------------------------------------------


class TestScreenshotFilename:
    """Tests for _screenshot_filename."""

    def test_canonical_example(self) -> None:
        """3723.456 s → 01꞉02꞉03․456_stem.png"""
        result = _screenshot_filename(3723.456, "stem")
        assert result == f"01{SEP}02{SEP}03{MSEP}456_stem.png"

    def test_zero_timestamp(self) -> None:
        """0 s → 00꞉00꞉00․000_stem.png"""
        result = _screenshot_filename(0.0, "stem")
        assert result == f"00{SEP}00{SEP}00{MSEP}000_stem.png"

    def test_one_hour(self) -> None:
        """3600 s → 01꞉00꞉00․000_stem.png"""
        result = _screenshot_filename(3600.0, "stem")
        assert result == f"01{SEP}00{SEP}00{MSEP}000_stem.png"

    def test_milliseconds_zero_padded(self) -> None:
        """Milliseconds < 10 are zero-padded to 3 digits (e.g. 008, not 8)."""
        # 1.008 * 1000 == 1008 exactly in IEEE 754
        result = _screenshot_filename(1.008, "v")
        assert result == f"00{SEP}00{SEP}01{MSEP}008_v.png"

    def test_seconds_zero_padded(self) -> None:
        """Seconds component is zero-padded to 2 digits."""
        result = _screenshot_filename(5.0, "v")
        assert result.startswith(f"00{SEP}00{SEP}05{MSEP}")

    def test_minutes_zero_padded(self) -> None:
        """Minutes component is zero-padded to 2 digits."""
        result = _screenshot_filename(60.0, "v")
        assert result.startswith(f"00{SEP}01{SEP}00{MSEP}")

    def test_uses_safe_separator(self) -> None:
        """Filename uses TIME_SEPARATOR_SAFE (꞉), not a regular colon."""
        result = _screenshot_filename(3723.0, "v")
        assert ":" not in result
        assert SEP in result

    def test_uses_ms_separator(self) -> None:
        """Filename uses TIME_SEPARATOR_MS (․), not a regular dot."""
        result = _screenshot_filename(3723.456, "v")
        assert MSEP in result

    def test_stem_included(self) -> None:
        """Video stem appears after the timestamp prefix."""
        result = _screenshot_filename(10.0, "my_video")
        assert result.endswith("_my_video.png")

    def test_png_extension(self) -> None:
        """Output always ends with .png."""
        result = _screenshot_filename(10.0, "clip")
        assert result.endswith(".png")

    def test_large_hours(self) -> None:
        """Hours > 99 are not truncated."""
        result = _screenshot_filename(360000.0, "v")  # 100 hours
        assert result.startswith(f"100{SEP}00{SEP}00{MSEP}")


# ---------------------------------------------------------------------------
# _resolve_crop
# ---------------------------------------------------------------------------

from pyqenc.models import CropParams
from pyqenc.phases.measure import _resolve_crop


class TestResolveCrop:
    """Tests for _resolve_crop."""

    # --- explicit CropParams passed in ---

    def test_explicit_crop_returned_unchanged(self, tmp_path: Path) -> None:
        """An explicit CropParams is returned as-is without touching job.yaml."""
        crop = CropParams(top=10, bottom=20, left=0, right=0)
        result = _resolve_crop(crop, tmp_path, tmp_path / "source.mkv")
        assert result is crop

    def test_explicit_empty_crop_returned_unchanged(self, tmp_path: Path) -> None:
        """An explicit empty CropParams (no-crop) is returned as-is."""
        crop = CropParams()
        result = _resolve_crop(crop, tmp_path, tmp_path / "source.mkv")
        assert result is crop

    # --- None with no job.yaml ---

    def test_none_no_probe_yaml_returns_empty_crop(self, tmp_path: Path, caplog) -> None:
        """None with no probe.yaml (and no job.yaml) returns empty CropParams and logs info."""
        with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
            result = _resolve_crop(None, tmp_path, tmp_path / "source.mkv")
        assert result == CropParams()
        assert any("No job.yaml" in r.message for r in caplog.records)

    # --- None with probe.yaml containing crop ---

    def test_none_probe_yaml_with_crop_returns_crop(self, tmp_path: Path) -> None:
        """None with a probe.yaml containing crop returns that crop without reading job.yaml."""
        from pyqenc.state import ProbeState

        expected_crop = CropParams(top=138, bottom=138, left=0, right=0)
        probe = ProbeState(frame_count=1000, crop=expected_crop)
        probe.save(tmp_path / "probe.yaml")

        result = _resolve_crop(None, tmp_path, tmp_path / "source.mkv")
        assert result == expected_crop

    # --- None with probe.yaml but no crop (falls back to job.yaml source check) ---

    def test_none_probe_yaml_no_crop_falls_back_to_job_yaml(self, tmp_path: Path, caplog) -> None:
        """None with probe.yaml (no crop) and non-matching job.yaml returns empty CropParams."""
        from pyqenc.state import ProbeState

        # probe.yaml exists but has no crop
        probe = ProbeState(frame_count=500, crop=None)
        probe.save(tmp_path / "probe.yaml")

        source = tmp_path / "source.mkv"
        other  = tmp_path / "other.mkv"

        mock_job = MagicMock()
        mock_job.source.path = other

        with patch("pyqenc.phases.measure.JobState") as mock_cls:
            mock_cls.load.return_value = mock_job
            with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
                result = _resolve_crop(None, tmp_path, source)

        assert result == CropParams()
        assert any("does not match" in r.message for r in caplog.records)

    # --- None with non-matching source in job.yaml ---

    def test_none_nonmatching_source_returns_empty_crop(self, tmp_path: Path, caplog) -> None:
        """None with a job.yaml whose source doesn't match returns empty CropParams."""
        source = tmp_path / "source.mkv"
        other  = tmp_path / "other.mkv"

        mock_job = MagicMock()
        mock_job.source.path = other

        with patch("pyqenc.phases.measure.JobState") as mock_cls:
            mock_cls.load.return_value = mock_job
            with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
                result = _resolve_crop(None, tmp_path, source)

        assert result == CropParams()
        assert any("does not match" in r.message for r in caplog.records)

    # --- None with job.yaml that has matching source but no crop in probe.yaml ---

    def test_none_no_crop_in_probe_yaml_returns_empty(self, tmp_path: Path, caplog) -> None:
        """None with matching job.yaml but no crop in probe.yaml returns empty CropParams."""
        source = tmp_path / "source.mkv"

        mock_job = MagicMock()
        mock_job.source.path = source

        with patch("pyqenc.phases.measure.JobState") as mock_cls:
            mock_cls.load.return_value = mock_job
            with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
                result = _resolve_crop(None, tmp_path, source)

        assert result == CropParams()
        assert any("no crop data" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _write_sidecar
# ---------------------------------------------------------------------------

import logging
from unittest.mock import patch

from pyqenc.phases.measure import _write_sidecar
from pyqenc.quality import MetricStats, MetricType


class TestWriteSidecar:
    """Tests for _write_sidecar failure handling."""

    def _make_metrics(self) -> dict:
        stats: MetricStats = {"min": 90.0, "p05": 91.0, "p25": 93.0, "median": 95.0, "p75": 97.0, "p95": 98.5, "max": 99.0, "std": 1.5}
        return {MetricType.VMAF: stats}

    def test_write_failure_logs_warning_and_does_not_raise(
        self, tmp_path: Path, caplog
    ) -> None:
        """OSError from write_yaml_atomic must be caught; warning logged; no exception."""
        with patch(
            "pyqenc.phases.measure.write_yaml_atomic",
            side_effect=OSError("disk full"),
        ):
            with caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"):
                # Must not raise
                _write_sidecar(
                    path                       = tmp_path / "target.yaml",
                    source_video               = tmp_path / "source.mkv",
                    target_video               = tmp_path / "target.mkv",
                    subsample_factor           = 10,
                    crop_params                = CropParams(top=0, bottom=0, left=0, right=0),
                    metrics                    = self._make_metrics(),
                    source_duration_seconds    = 100.0,
                    target_duration_seconds    = 98.0,
                    effective_duration_seconds = 98.0,
                )

        assert any("Failed to write metrics sidecar" in r.message for r in caplog.records)

    def test_write_success_creates_no_tmp_file(self, tmp_path: Path) -> None:
        """On success the final file exists and no .tmp file is left behind."""
        sidecar = tmp_path / "target.yaml"
        _write_sidecar(
            path                       = sidecar,
            source_video               = tmp_path / "source.mkv",
            target_video               = tmp_path / "target.mkv",
            subsample_factor           = 10,
            crop_params                = CropParams(top=138, bottom=138, left=0, right=0),
            metrics                    = self._make_metrics(),
            source_duration_seconds    = 100.0,
            target_duration_seconds    = 98.0,
            effective_duration_seconds = 98.0,
        )

        assert sidecar.exists()
        assert not (tmp_path / "target.tmp").exists()

    def test_write_success_contains_expected_fields(self, tmp_path: Path) -> None:
        """Written YAML contains all required top-level fields."""
        import yaml

        sidecar = tmp_path / "target.yaml"
        _write_sidecar(
            path                       = sidecar,
            source_video               = tmp_path / "source.mkv",
            target_video               = tmp_path / "target.mkv",
            subsample_factor           = 5,
            crop_params                = CropParams(top=10, bottom=20, left=0, right=0),
            metrics                    = self._make_metrics(),
            source_duration_seconds    = 200.0,
            target_duration_seconds    = None,
            effective_duration_seconds = None,
        )

        data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        assert "source_video" in data
        assert "target_video" in data
        assert "source_duration_seconds" in data
        assert "target_duration_seconds" in data
        assert "effective_duration_seconds" in data
        assert "sampling" in data
        assert "crop_params" in data
        assert "metrics" in data
        assert data["sampling"] == 5
        assert data["target_duration_seconds"] is None
        assert data["crop_params"] == {"top": 10, "bottom": 20, "left": 0, "right": 0}
        # metrics are flat: vmaf_min, vmaf_median, etc.
        assert f"{MetricType.VMAF.value}_min" in data["metrics"]


# ---------------------------------------------------------------------------
# make_screenshots
# ---------------------------------------------------------------------------

import asyncio
from fractions import Fraction
from unittest.mock import AsyncMock, MagicMock, patch

from pyqenc.phases.measure import ScreenshotPositions, make_screenshots
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult


def _make_ffmpeg_result(success: bool = True, returncode: int = 0) -> FFmpegRunResult:
    return FFmpegRunResult(returncode=returncode, success=success, stderr_lines=[], frame_count=None)


def _make_positions(frame_nums: list[int], fps: Fraction = Fraction(24, 1), step: int = 10) -> ScreenshotPositions:
    return ScreenshotPositions(frame_nums=frame_nums, fps=fps, step=step)


class TestMakeScreenshots:
    """Unit tests for make_screenshots (Strategy C primary, A2/A4 fallbacks)."""

    def test_empty_positions_returns_empty(self, tmp_path: Path) -> None:
        """Empty frame_nums returns [] immediately without calling ffmpeg."""
        positions = _make_positions([])
        result = asyncio.run(
            make_screenshots(
                video_path      = tmp_path / "video.mkv",
                positions       = positions,
                screenshots_dir = tmp_path,
            )
        )
        assert result == []

    def test_strategy_c_success_returns_named_paths(self, tmp_path: Path) -> None:
        """Strategy C success: returns list of final named screenshot paths."""
        positions = _make_positions([240, 480])

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            # Strategy C writes directly to the tmp_path output file
            cmd_strs = [str(a) for a in cmd]
            # The output path is the last arg — create it
            out = Path(cmd_strs[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PNG")
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            result = asyncio.run(
                make_screenshots(
                    video_path      = tmp_path / "video.mkv",
                    positions       = positions,
                    screenshots_dir = tmp_path,
                )
            )

        assert len(result) == 2
        for p in result:
            assert p.suffix == ".png"
            assert "video" in p.name

    def test_strategy_c_uses_fast_seek(self, tmp_path: Path) -> None:
        """Strategy C places -ss before -i in the ffmpeg command."""
        positions = _make_positions([240])
        captured_cmds: list[list[str]] = []

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            captured_cmds.append([str(a) for a in cmd])
            out = Path(str(cmd[-1]))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PNG")
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            asyncio.run(
                make_screenshots(
                    video_path      = tmp_path / "video.mkv",
                    positions       = positions,
                    screenshots_dir = tmp_path,
                )
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert cmd[0] == "ffmpeg"
        assert "-ss" in cmd
        assert "-i" in cmd
        # -ss must come before -i
        assert cmd.index("-ss") < cmd.index("-i")

    def test_strategy_c_crop_included_when_non_empty(self, tmp_path: Path) -> None:
        """Non-empty crop params are included in Strategy C vf filter."""
        positions = _make_positions([240])
        captured_cmds: list[list[str]] = []

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            captured_cmds.append([str(a) for a in cmd])
            out = Path(str(cmd[-1]))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PNG")
            return _make_ffmpeg_result(success=True)

        crop = CropParams(top=138, bottom=138, left=0, right=0)
        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            asyncio.run(
                make_screenshots(
                    video_path      = tmp_path / "video.mkv",
                    positions       = positions,
                    screenshots_dir = tmp_path,
                    crop_params     = crop,
                )
            )

        cmd = captured_cmds[0]
        assert "-vf" in cmd
        vf_val = cmd[cmd.index("-vf") + 1]
        assert "crop=" in vf_val

    def test_strategy_c_zero_output_falls_back_to_a2(self, tmp_path: Path, caplog) -> None:
        """When Strategy C yields zero files, A2 fallback is attempted and warning logged."""
        positions = _make_positions([240, 480])
        call_count = 0

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd_strs = [str(a) for a in cmd]
            # Strategy C calls: success=True but output file NOT created → zero output
            # Strategy A2 call: create output files in tmp_dir
            if "-vsync" in cmd_strs:
                # This is A2 or A4 single-pass
                out_pattern = Path(cmd_strs[-1])
                tmp_dir = out_pattern.parent
                tmp_dir.mkdir(parents=True, exist_ok=True)
                (tmp_dir / "0001.png").write_bytes(b"PNG1")
                (tmp_dir / "0002.png").write_bytes(b"PNG2")
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            with caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"):
                result = asyncio.run(
                    make_screenshots(
                        video_path      = tmp_path / "video.mkv",
                        positions       = positions,
                        screenshots_dir = tmp_path,
                    )
                )

        assert any("falling back to A2" in r.message for r in caplog.records)
        assert len(result) == 2

    def test_strategy_a2_zero_output_falls_back_to_a4(self, tmp_path: Path, caplog) -> None:
        """When A2 also yields zero files, A4 fallback is attempted and warning logged."""
        positions = _make_positions([240, 480])
        a4_called = False

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            nonlocal a4_called
            cmd_strs = [str(a) for a in cmd]
            if "-vsync" in cmd_strs:
                vf_val = cmd_strs[cmd_strs.index("-vf") + 1] if "-vf" in cmd_strs else ""
                if "mod(" in vf_val:
                    # A4 call — produce output
                    a4_called = True
                    out_pattern = Path(cmd_strs[-1])
                    tmp_dir = out_pattern.parent
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    (tmp_dir / "0001.png").write_bytes(b"PNG1")
                    (tmp_dir / "0002.png").write_bytes(b"PNG2")
                # A2 call — produce no output (zero files)
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            with caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"):
                result = asyncio.run(
                    make_screenshots(
                        video_path      = tmp_path / "video.mkv",
                        positions       = positions,
                        screenshots_dir = tmp_path,
                    )
                )

        assert any("falling back to A4" in r.message for r in caplog.records)
        assert a4_called
        assert len(result) == 2

    def test_all_strategies_fail_logs_error_returns_empty(self, tmp_path: Path, caplog) -> None:
        """When all strategies yield zero output, ERROR is logged and [] returned."""
        positions = _make_positions([240])

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            return _make_ffmpeg_result(success=True)  # success but no files created

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            with caplog.at_level(logging.ERROR, logger="pyqenc.phases.measure"):
                result = asyncio.run(
                    make_screenshots(
                        video_path      = tmp_path / "video.mkv",
                        positions       = positions,
                        screenshots_dir = tmp_path,
                    )
                )

        assert result == []
        assert any("All screenshot strategies failed" in r.message for r in caplog.records)

    def test_partial_results_not_triggering_fallback(self, tmp_path: Path, caplog) -> None:
        """Partial results (some frames captured) do NOT trigger A2 fallback."""
        positions = _make_positions([240, 480, 720])
        call_count = 0

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd_strs = [str(a) for a in cmd]
            # Strategy C: only first frame produces output
            if "-ss" in cmd_strs and "-frames:v" in cmd_strs:
                out = Path(cmd_strs[-1])
                if call_count == 1:  # only first call creates file
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(b"PNG")
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            with caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"):
                result = asyncio.run(
                    make_screenshots(
                        video_path      = tmp_path / "video.mkv",
                        positions       = positions,
                        screenshots_dir = tmp_path,
                    )
                )

        # Partial result (1 of 3) — no A2 fallback
        assert len(result) == 1
        assert not any("falling back to A2" in r.message for r in caplog.records)

    def test_no_tmp_files_left_after_success(self, tmp_path: Path) -> None:
        """No .tmp files or temp dirs remain after successful Strategy C capture."""
        positions = _make_positions([240])

        async def fake_ffmpeg(cmd, output_file, **kwargs):
            out = Path(str(cmd[-1]))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PNG")
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            asyncio.run(
                make_screenshots(
                    video_path      = tmp_path / "video.mkv",
                    positions       = positions,
                    screenshots_dir = tmp_path,
                )
            )

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"
        tmp_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith(".tmp_")]
        assert tmp_dirs == [], f"Unexpected temp dirs: {tmp_dirs}"
