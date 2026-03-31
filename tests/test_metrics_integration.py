"""Phase integration tests for MetricsCollector timing instrumentation.

Verifies that phases call ``record_step`` with the expected ``TimeKey`` values
when their core work methods run.  External I/O (ffprobe, ffmpeg, crop detect)
is mocked out so tests run without real media files.

Tests live here per the spec: tests/test_metrics_integration.py
"""
# CHerSun 2026

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.metrics import MetricsCollector, NoOpMetricsCollector, TimeKey
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PipelineConfig,
    VideoMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, *, crop_params: CropParams | None = None) -> PipelineConfig:
    """Return a minimal ``PipelineConfig`` with a stub source file."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    return PipelineConfig(
        source_video    = source,
        work_dir        = tmp_path / "work",
        quality_targets = [],
        strategies      = [],
        optimize        = False,
        max_parallel    = 1,
        include         = None,
        exclude         = None,
        cleanup         = CleanupLevel.NONE,
        chunking_mode   = ChunkingMode.LOSSLESS,
        force           = False,
        crop_params     = crop_params,
    )


def _spy_collector() -> MagicMock:
    """Return a ``MagicMock`` that satisfies the ``MetricsCollector`` Protocol.

    ``time()`` returns a real no-op context manager so ``with collector.time(key):``
    works correctly in phase code.  ``record_step`` is a plain mock so calls
    can be inspected via ``assert_called_with`` / ``call_args_list``.
    """
    collector = MagicMock(spec=MetricsCollector)
    collector.time.return_value = contextlib.nullcontext()
    return collector


def _stub_video_metadata(source: Path) -> VideoMetadata:
    """Return a ``VideoMetadata`` with all private fields pre-populated.

    Avoids any real ffprobe / ffmpeg calls during tests.
    """
    vm = VideoMetadata(path=source)
    vm._file_size_bytes  = 64
    vm._duration_seconds = 120.0
    vm._fps              = 24.0
    vm._resolution       = "1920x1080"
    vm._frame_count      = 2880
    return vm


# ---------------------------------------------------------------------------
# JobPhase — JOB_PROBE timing
# ---------------------------------------------------------------------------

class TestJobPhaseTiming:
    """Integration tests for ``JobPhase`` timing instrumentation (Req 6.5)."""

    def test_job_probe_recorded_on_run(self, tmp_path: Path) -> None:
        """``record_step`` must be called with ``TimeKey.JOB_PROBE`` when ``run()`` executes.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path)
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        # Patch VideoMetadata so no real ffprobe/ffmpeg calls happen.
        # Patch detect_crop_parameters to return a zero-crop immediately.
        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=CropParams()),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        # Verify time() was called with JOB_PROBE
        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_PROBE in time_keys_called, (
            f"Expected TimeKey.JOB_PROBE in time() calls, got: {time_keys_called}"
        )

    def test_job_probe_not_recorded_when_reused(self, tmp_path: Path) -> None:
        """When ``job.yaml`` already exists and source matches, probing is skipped.

        The phase returns REUSED without re-probing, so ``time(JOB_PROBE)``
        must NOT be called.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase
        from pyqenc.state import JobState

        config    = _make_config(tmp_path)
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        # Pre-create a valid job.yaml so the phase takes the REUSED path.
        config.work_dir.mkdir(parents=True, exist_ok=True)
        stub_vm = _stub_video_metadata(config.source_video)
        job = JobState(source=stub_vm, crop=CropParams())
        job.save(config.work_dir / "job.yaml")

        with (
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_PROBE not in time_keys_called, (
            f"Expected JOB_PROBE NOT called on reuse, but got: {time_keys_called}"
        )

    def test_job_crop_detect_recorded_when_no_cache(self, tmp_path: Path) -> None:
        """``time(JOB_CROP_DETECT)`` must be called when auto-detect runs.

        Auto-detect runs when neither manual crop nor cached crop is available.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path)  # crop_params=None → auto-detect path
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=CropParams()),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_CROP_DETECT in time_keys_called, (
            f"Expected TimeKey.JOB_CROP_DETECT in time() calls, got: {time_keys_called}"
        )

    def test_job_crop_detect_not_recorded_for_manual_crop(self, tmp_path: Path) -> None:
        """``time(JOB_CROP_DETECT)`` must NOT be called when manual crop is set.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path, crop_params=CropParams(top=140, bottom=140))
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_CROP_DETECT not in time_keys_called, (
            f"Expected JOB_CROP_DETECT NOT called for manual crop, got: {time_keys_called}"
        )

    def test_job_crop_detect_not_recorded_for_cached_crop(self, tmp_path: Path) -> None:
        """``time(JOB_CROP_DETECT)`` must NOT be called when crop is cached in job.yaml.

        Validates: Requirements 6.5
        """
        from pyqenc.phases.job import JobPhase
        from pyqenc.state import JobState

        config    = _make_config(tmp_path)  # no manual crop
        collector = _spy_collector()
        phase     = JobPhase(config, collector=collector)

        # Pre-create job.yaml with a cached crop — phase will load it and skip detect.
        config.work_dir.mkdir(parents=True, exist_ok=True)
        stub_vm = _stub_video_metadata(config.source_video)
        job = JobState(source=stub_vm, crop=CropParams(top=100, bottom=100))
        job.save(config.work_dir / "job.yaml")

        with (
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            phase.run()

        time_keys_called = [call.args[0] for call in collector.time.call_args_list]
        assert TimeKey.JOB_CROP_DETECT not in time_keys_called, (
            f"Expected JOB_CROP_DETECT NOT called for cached crop, got: {time_keys_called}"
        )

    def test_noop_collector_works_as_drop_in(self, tmp_path: Path) -> None:
        """``JobPhase`` must run without error when given a ``NoOpMetricsCollector``.

        Validates: Requirements 6.4, 6.5
        """
        from pyqenc.phases.job import JobPhase

        config    = _make_config(tmp_path)
        collector = NoOpMetricsCollector()
        phase     = JobPhase(config, collector=collector)

        stub_vm = _stub_video_metadata(config.source_video)

        with (
            patch("pyqenc.phases.job.VideoMetadata", return_value=stub_vm),
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=CropParams()),
            patch("pyqenc.phases.job.log_disk_space_info"),
        ):
            result = phase.run()

        assert result is not None
