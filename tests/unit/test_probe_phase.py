"""Unit tests for ProbePhase.run() — three observable paths.

Covers:
- FAILED path: extraction_result.video is None → phase returns FAILED
- REUSED path: probe.yaml fully cached, no --crop override → returns REUSED
- COMPLETED path: probe.yaml absent → detection runs (mocked) → returns COMPLETED
"""
# CHerSun 2026

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.app_config import load_app_config
from pyqenc.models import (
    CropParams,
    ExtendedVideoMetadata,
    PhaseOutcome,
    VideoMetadata,
)
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState, ProbeState


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_APP_CONFIG = load_app_config(default_only=True)


def _make_source_vm(path: Path) -> VideoMetadata:
    meta = VideoMetadata(path=path)
    meta._duration_seconds = 3600.0
    meta._fps              = 24.0
    meta._resolution       = "1920x1080"
    return meta


def _make_job_state(source_vm: VideoMetadata):
    """Return a minimal JobState wrapping the given VideoMetadata."""
    from pyqenc.state import JobState
    return JobState(source=source_vm)


def _make_job_result(work_dir: Path, source_vm: VideoMetadata):
    """Return a completed JobPhaseResult with the given work_dir and source."""
    from pyqenc.phases.job import JobPhaseResult
    from pyqenc.models import PhaseOutcome
    from pyqenc.phase import Artifact
    from pyqenc.state import ArtifactState

    job_state = _make_job_state(source_vm)

    return JobPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        artifacts = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
        message   = "job complete",
        job       = job_state,
        work_dir  = work_dir,
        source    = source_vm.path,
    )


def _make_extraction_result(video: VideoMetadata | None):
    """Return an ExtractionPhaseResult with the given video."""
    from pyqenc.phases.extraction import ExtractionPhaseResult
    from pyqenc.models import PhaseOutcome
    from pyqenc.phase import Artifact
    from pyqenc.state import ArtifactState

    if video is not None:
        return ExtractionPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [Artifact(path=video.path, state=ArtifactState.COMPLETE)],
            message   = "extraction complete",
            video     = video,
        )
    else:
        return ExtractionPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [],
            message   = "extraction complete — no video",
            video     = None,
        )


def _make_probe_phase(
    job_result,
    extraction_result,
    *,
    crop_params: CropParams | None = None,
) -> ProbePhase:
    """Build a ProbePhase with pre-populated dependency results."""
    collector = MagicMock()

    job_mock        = MagicMock()
    job_mock.result = job_result

    extraction_mock        = MagicMock()
    extraction_mock.result = extraction_result

    # Patch the private dependency attributes directly
    phase = ProbePhase.__new__(ProbePhase)
    phase._config      = _APP_CONFIG
    phase._collector   = collector
    phase._crop_params = crop_params
    phase._job         = job_mock
    phase._extraction  = extraction_mock
    phase.result       = None
    phase.dependencies = [job_mock, extraction_mock]

    # Patch _ensure_dependencies so the mock phase objects aren't traversed
    def _passthrough_ensure(execute: bool) -> None:
        return None  # dependencies are already "run"

    phase._ensure_dependencies = _passthrough_ensure  # type: ignore[method-assign]

    return phase


# ---------------------------------------------------------------------------
# FAILED path: no video extracted
# ---------------------------------------------------------------------------

class TestProbePhaseFailedNoVideo:
    """ProbePhase.run() must return FAILED when extraction produced no video.

    Bug: if the None check were missing, the phase would proceed to crop
    detection on a None path and raise an AttributeError deep in ffprobe.
    """

    def test_returns_failed_outcome(self, tmp_path: Path):
        """Outcome must be FAILED when extraction_result.video is None."""
        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        job_result        = _make_job_result(tmp_path / "work", source_vm)
        extraction_result = _make_extraction_result(video=None)
        phase             = _make_probe_phase(job_result, extraction_result)

        result = phase.run()

        assert result.outcome == PhaseOutcome.FAILED

    def test_source_is_none_on_failed(self, tmp_path: Path):
        """ProbePhaseResult.source must be None when no video was extracted."""
        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        job_result        = _make_job_result(tmp_path / "work", source_vm)
        extraction_result = _make_extraction_result(video=None)
        phase             = _make_probe_phase(job_result, extraction_result)

        result = phase.run()

        assert result.source is None

    def test_error_field_populated_on_failed(self, tmp_path: Path):
        """result.error must be populated when outcome is FAILED."""
        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        job_result        = _make_job_result(tmp_path / "work", source_vm)
        extraction_result = _make_extraction_result(video=None)
        phase             = _make_probe_phase(job_result, extraction_result)

        result = phase.run()

        assert result.error is not None
        assert len(result.error) > 0

    def test_probe_yaml_not_written_on_failed(self, tmp_path: Path):
        """probe.yaml must not be created when phase returns FAILED.

        Bug: writing probe.yaml on failure could confuse recovery — a
        subsequent run would think probing succeeded and skip re-probing.
        """
        work_dir          = tmp_path / "work"
        work_dir.mkdir()
        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=None)
        phase             = _make_probe_phase(job_result, extraction_result)

        phase.run()

        assert not (work_dir / "probe.yaml").exists()


# ---------------------------------------------------------------------------
# REUSED path: probe.yaml fully cached, no --crop override
# ---------------------------------------------------------------------------

class TestProbePhaseReused:
    """ProbePhase.run() must return REUSED when probe.yaml is fully cached.

    Bug: if the REUSED shortcut were missing, the phase would run crop
    detection and frame counting on every invocation even when results exist.
    """

    def _write_probe_yaml(self, work_dir: Path, frame_count: int = 1440) -> None:
        """Write a valid probe.yaml with crop to work_dir."""
        state = ProbeState(
            frame_count = frame_count,
            crop        = CropParams(top=140, bottom=140),
        )
        state.save(work_dir / "probe.yaml")

    def test_returns_reused_outcome(self, tmp_path: Path):
        """Outcome must be REUSED when probe.yaml has frame_count > 0 and crop set."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        self._write_probe_yaml(work_dir)

        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm      = _make_source_vm(tmp_path / "extracted.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=extracted_vm)
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        result = phase.run()

        assert result.outcome == PhaseOutcome.REUSED

    def test_reused_result_carries_cached_frame_count(self, tmp_path: Path):
        """ProbePhaseResult.source.frame_count must match the cached value."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        self._write_probe_yaml(work_dir, frame_count=72000)

        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm      = _make_source_vm(tmp_path / "extracted.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=extracted_vm)
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        result = phase.run()

        assert result.source is not None
        assert result.source.frame_count == 72000

    def test_reused_result_carries_cached_crop(self, tmp_path: Path):
        """ProbePhaseResult.crop must match the cached CropParams."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        self._write_probe_yaml(work_dir)

        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm      = _make_source_vm(tmp_path / "extracted.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=extracted_vm)
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        result = phase.run()

        assert result.crop.top    == 140
        assert result.crop.bottom == 140

    def test_crop_override_bypasses_reused(self, tmp_path: Path):
        """A manual --crop override must prevent the REUSED shortcut.

        Bug: if crop_params were ignored when probe.yaml is present, the
        user's explicit crop override would be silently discarded.
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        self._write_probe_yaml(work_dir)

        source_vm         = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm      = _make_source_vm(tmp_path / "extracted.mkv")
        job_result        = _make_job_result(work_dir, source_vm)
        extraction_result = _make_extraction_result(video=extracted_vm)
        override_crop     = CropParams(top=0, bottom=0)

        with (
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=override_crop),
            patch.object(VideoMetadata, "probe_extended", return_value=ExtendedVideoMetadata.from_base(source_vm, frame_count=1440)),
        ):
            phase  = _make_probe_phase(job_result, extraction_result, crop_params=override_crop)
            result = phase.run()

        # Must NOT be REUSED — the override forces re-processing
        assert result.outcome != PhaseOutcome.REUSED


# ---------------------------------------------------------------------------
# COMPLETED path: probe.yaml absent → full detection
# ---------------------------------------------------------------------------

class TestProbePhaseCompleted:
    """ProbePhase.run() must return COMPLETED after running detection.

    Bug: without the COMPLETED path, fresh runs would fail silently because
    neither crop detection nor frame count probing would ever execute.
    """

    _DETECTED_CROP        = CropParams(top=140, bottom=140)
    _DETECTED_FRAME_COUNT = 1440

    def _run_with_mocks(self, tmp_path: Path) -> tuple[ProbePhaseResult, Path]:
        """Helper that runs ProbePhase with detection mocked out.

        Returns:
            (result, work_dir)
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        source_vm    = _make_source_vm(tmp_path / "source.mkv")
        extracted_vm = _make_source_vm(tmp_path / "extracted.mkv")
        job_result   = _make_job_result(work_dir, source_vm)

        extended_vm = ExtendedVideoMetadata.from_base(
            source_vm, frame_count=self._DETECTED_FRAME_COUNT
        )

        extraction_result = _make_extraction_result(video=extracted_vm)
        phase             = _make_probe_phase(job_result, extraction_result, crop_params=None)

        with (
            patch("pyqenc.utils.crop.detect_crop_parameters", return_value=self._DETECTED_CROP),
            patch.object(VideoMetadata, "probe_extended", return_value=extended_vm),
        ):
            result = phase.run()

        return result, work_dir

    def test_returns_completed_outcome(self, tmp_path: Path):
        """Outcome must be COMPLETED when probe.yaml is absent and detection runs."""
        result, _ = self._run_with_mocks(tmp_path)
        assert result.outcome == PhaseOutcome.COMPLETED

    def test_result_carries_detected_frame_count(self, tmp_path: Path):
        """ProbePhaseResult.source.frame_count must reflect what probe_extended returned."""
        result, _ = self._run_with_mocks(tmp_path)
        assert result.source is not None
        assert result.source.frame_count == self._DETECTED_FRAME_COUNT

    def test_result_carries_detected_crop(self, tmp_path: Path):
        """ProbePhaseResult.crop must reflect what detect_crop_parameters returned."""
        result, _ = self._run_with_mocks(tmp_path)
        assert result.crop.top    == self._DETECTED_CROP.top
        assert result.crop.bottom == self._DETECTED_CROP.bottom

    def test_probe_yaml_written_after_completed(self, tmp_path: Path):
        """probe.yaml must exist on disk after a COMPLETED run.

        Bug: if probe.yaml were not written, every subsequent run would
        re-probe and re-detect instead of reusing the cached result.
        """
        result, work_dir = self._run_with_mocks(tmp_path)
        assert (work_dir / "probe.yaml").exists()

    def test_probe_yaml_round_trips_detected_values(self, tmp_path: Path):
        """The probe.yaml written by COMPLETED must contain the detected values.

        Bug: if detection results were not persisted, the REUSED path on
        the next run would load stale or zero values.
        """
        _, work_dir = self._run_with_mocks(tmp_path)
        loaded = ProbeState.load(work_dir / "probe.yaml")
        assert loaded is not None
        assert loaded.frame_count           == self._DETECTED_FRAME_COUNT
        assert loaded.crop is not None
        assert loaded.crop.top              == self._DETECTED_CROP.top

    def test_source_is_extended_video_metadata(self, tmp_path: Path):
        """result.source must be an ExtendedVideoMetadata instance.

        Bug: returning a plain VideoMetadata would prevent frame_count access
        and could cause AttributeError downstream.
        """
        result, _ = self._run_with_mocks(tmp_path)
        assert isinstance(result.source, ExtendedVideoMetadata)
