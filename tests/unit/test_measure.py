"""Unit tests for pyqenc/phases/measure.py helper functions.

Covers: _parse_duration, _resolve_crop, make_screenshots.

Run with: uv run python -m pytest tests/unit/test_measure.py
"""

# Feature: standalone-measure

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from pyqenc.phases.measure import (
    _parse_duration,
)

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
# _resolve_crop
# ---------------------------------------------------------------------------

from pyqenc.models import CropParams
from pyqenc.phases.measure import _resolve_crop
from pyqenc.stream_model import File, JobSidecar
from pyqenc.utils.yaml_utils import write_yaml_atomic


def _seed_job_yaml(work_dir: Path, source: Path) -> None:
    """Write a job.yaml (the File dump) recording the given source path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    sidecar = JobSidecar(source=File(path=source, file_size_bytes=None))
    write_yaml_atomic(work_dir / "job.yaml", sidecar.model_dump(exclude_none=True))


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

    # --- None with probe.yaml but empty crop (a concrete "no crop" resolution) ---

    def test_none_probe_yaml_empty_crop_resolves_directly(self, tmp_path: Path, caplog) -> None:
        """None with probe.yaml whose crop is empty returns empty CropParams
        immediately — an empty crop is a concrete resolution, never a reason
        to keep probing the job.yaml fallback."""
        from pyqenc.state import ProbeState

        probe = ProbeState(frame_count=500)
        probe.save(tmp_path / "probe.yaml")

        source = tmp_path / "source.mkv"
        _seed_job_yaml(tmp_path, tmp_path / "other.mkv")

        with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
            result = _resolve_crop(None, tmp_path, source)

        assert result == CropParams()
        assert not any("does not match" in r.message for r in caplog.records)

    # --- None with non-matching source in job.yaml ---

    def test_none_nonmatching_source_returns_empty_crop(self, tmp_path: Path, caplog) -> None:
        """None with a job.yaml whose source doesn't match returns empty CropParams."""
        source = tmp_path / "source.mkv"
        other  = tmp_path / "other.mkv"
        _seed_job_yaml(tmp_path, other)

        with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
            result = _resolve_crop(None, tmp_path, source)

        assert result == CropParams()
        assert any("does not match" in r.message for r in caplog.records)

    # --- None with job.yaml that has matching source but no crop in probe.yaml ---

    def test_none_no_crop_in_probe_yaml_returns_empty(self, tmp_path: Path, caplog) -> None:
        """None with matching job.yaml but no crop in probe.yaml returns empty CropParams."""
        source = tmp_path / "source.mkv"
        _seed_job_yaml(tmp_path, source)

        with caplog.at_level(logging.INFO, logger="pyqenc.phases.measure"):
            result = _resolve_crop(None, tmp_path, source)

        assert result == CropParams()
        assert any("no crop data" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _write_sidecar
# ---------------------------------------------------------------------------


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
        ), caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"):
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
        assert "target_duration_seconds" not in data  # None → omitted (exclude_none)
        assert "effective_duration_seconds" not in data  # None → omitted (exclude_none)
        assert "sampling" in data
        assert "crop_params" in data
        assert "metrics" in data
        assert data["sampling"] == 5
        assert "target_duration_seconds" not in data  # None → omitted (exclude_none)
        assert data["crop_params"] == {"top": 10, "bottom": 20, "left": 0, "right": 0}
        # metrics are flat: vmaf_min, vmaf_median, etc.
        assert f"{MetricType.VMAF.value}_min" in data["metrics"]


# ---------------------------------------------------------------------------
# make_screenshots
# ---------------------------------------------------------------------------

import asyncio
from fractions import Fraction

from pyqenc.phases.measure import (
    ScreenshotPositions,
    _capture_single_frame,
    _capture_single_pass,
    make_screenshots,
)
from pyqenc.utils.ffmpeg_runner import (
    _PROGRESS_FLAGS,
    FFmpegRequest,
    FFmpegRunResult,
    compose_command,
)


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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            # Strategy C targets the per-frame output path — create it
            out = Path(str(request.output))
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            captured_cmds.append([str(a) for a in compose_command(request)[0]])
            out = Path(str(request.output))
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            captured_cmds.append([str(a) for a in compose_command(request)[0]])
            out = Path(str(request.output))
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            # Strategy C (request.output set): success=True but output NOT created → zero output
            # Single-pass fallback (output None): create output files in the pattern's tmp dir
            if request.output is None:
                pattern = Path(str(request.output_args[-1]))
                tmp_dir = pattern.parent
                tmp_dir.mkdir(parents=True, exist_ok=True)
                (tmp_dir / "0001.png").write_bytes(b"PNG1")
                (tmp_dir / "0002.png").write_bytes(b"PNG2")
            return _make_ffmpeg_result(success=True)

        with (
            patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg),
            caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"),
        ):
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            nonlocal a4_called
            if request.output is None:
                args = [str(a) for a in request.output_args]
                vf_val = args[args.index("-vf") + 1] if "-vf" in args else ""
                if "mod(" in vf_val:
                    # A4 call — produce output
                    a4_called = True
                    pattern = Path(args[-1])
                    tmp_dir = pattern.parent
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    (tmp_dir / "0001.png").write_bytes(b"PNG1")
                    (tmp_dir / "0002.png").write_bytes(b"PNG2")
                # A2 call — produce no output (zero files)
            return _make_ffmpeg_result(success=True)

        with (
            patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg),
            caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"),
        ):
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            return _make_ffmpeg_result(success=True)  # success but no files created

        with (
            patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg),
            caplog.at_level(logging.ERROR, logger="pyqenc.phases.measure"),
        ):
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            nonlocal call_count
            call_count += 1
            # Strategy C (request.output set): only first frame produces output
            if request.output is not None and call_count == 1:
                out = Path(str(request.output))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"PNG")
            return _make_ffmpeg_result(success=True)

        with (
            patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg),
            caplog.at_level(logging.WARNING, logger="pyqenc.phases.measure"),
        ):
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

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            out = Path(str(request.output))
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


# ---------------------------------------------------------------------------
# Screenshot capture — golden composed argv (both ffmpeg call sites)
# ---------------------------------------------------------------------------

class TestScreenshotCommandGolden:
    """Pin the composed argv for the two screenshot capture call sites.

    Bug prevented: the request-model conversion drifting from the original
    hand-built commands — fast-seek window, vf chain, frame cap, image2 output.
    The single-frame capture now routes through the runner's ``.tmp`` protocol
    with an explicit ``image2`` muxer (the pre-request code wrote the final
    path directly); the multi-frame pattern keeps writing its caller-managed
    temp dir verbatim.
    """

    def test_single_frame_golden_argv(self) -> None:
        captured: list[FFmpegRequest] = []

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            captured.append(request)
            return _make_ffmpeg_result(success=True)

        video = Path("/v/video.mkv")
        out   = Path("/shots/00_video.png")
        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            asyncio.run(_capture_single_frame(video, "9.989583333", out, None))

        assert len(captured) == 1
        argv = [str(a) for a in compose_command(captured[0])[0]]
        assert argv == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-ss", "9.989583333",
            "-i", str(video),
            "-frames:v", "1",
            "-c:v", "png",
            "-map_chapters", "-1",
            "-f", "image2", str(Path("/shots/00_video.tmp")),
        ]

    def test_single_frame_crop_in_vf(self) -> None:
        crop = CropParams(top=1, bottom=1, left=0, right=0)
        captured: list[FFmpegRequest] = []

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            captured.append(request)
            return _make_ffmpeg_result(success=True)

        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            asyncio.run(_capture_single_frame(Path("/v/video.mkv"), "9.989583333", Path("/shots/00.png"), crop))

        argv = [str(a) for a in compose_command(captured[0])[0]]
        vf_idx = argv.index("-vf")
        assert argv[vf_idx + 1] == "crop=iw-0:ih-2:0:1"

    def test_single_pass_golden_argv(self) -> None:
        captured: list[FFmpegRequest] = []

        async def fake_ffmpeg(request: FFmpegRequest, **kwargs):
            captured.append(request)
            return _make_ffmpeg_result(success=True)

        video   = Path("/v/video.mkv")
        tmp_dir = Path("/shots/tmpdir")
        with patch("pyqenc.phases.measure.run_ffmpeg_async", side_effect=fake_ffmpeg):
            asyncio.run(_capture_single_pass(video, "not(mod(n,240))", tmp_dir, None))

        assert len(captured) == 1
        argv = [str(a) for a in compose_command(captured[0])[0]]
        assert argv == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-i", str(video),
            "-vf", "select='not(mod(n,240))',setpts=N/FRAME_RATE/TB",
            "-vsync", "0",
            str(tmp_dir / "%04d.png"),
            "-map_chapters", "-1",
            "-f", "null", "-",
        ]
